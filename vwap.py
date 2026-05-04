"""
Production ETH/USDT Futures Bot
Strategy : EMA trend + VWAP pullback + RSI filter
Interval : 1m  (configurable)
"""

import os
import sys
import time
import math
import json
import signal
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from functools import wraps
from datetime import date
from collections import deque
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException

# =============================================================
# CONFIG
# =============================================================
load_dotenv()

API_KEY    = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL     = "ETHUSDT"
INTERVAL   = Client.KLINE_INTERVAL_1MINUTE

LEVERAGE   = 2
RISK_PCT   = 0.10           # fraction of wallet risked per trade
MAX_DAILY_LOSS_PCT = 0.05   # halt if daily loss exceeds 5%

EMA_PERIOD   = 198
RSI_PERIOD   = 15
ATR_PERIOD   = 15
VWAP_WINDOW  = 51

TP_MULT   = 1.5
SL_MULT   = 1.0

LONG_RSI_MAX  = 45    # RSI must be BELOW this to enter long
SHORT_RSI_MIN = 55    # RSI must be ABOVE this to enter short
VWAP_BAND     = 0.01  # price must be within 1% of VWAP

STATE_FILE         = "bot_state.json"
LOG_FILE           = "bot.log"
WATCHDOG_TIMEOUT_S = 180   # restart websocket if silent for 3 min
FILL_POLL_INTERVAL = 0.5   # seconds between fill-status polls
FILL_POLL_RETRIES  = 10    # total fill wait = 5 s


# =============================================================
# LOGGING
# =============================================================
def _build_logger() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    root.addHandler(ch)
    root.addHandler(fh)
    return logging.getLogger("bot")

log = _build_logger()


# =============================================================
# RETRY DECORATOR
# =============================================================
def retry(max_attempts: int = 3, base_delay: float = 1.5, backoff: float = 2.0):
    """Exponential back-off retry for Binance API calls."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempts:
                        log.error(
                            f"[RETRY] {fn.__name__} gave up after "
                            f"{max_attempts} attempts: {exc}"
                        )
                        raise
                    log.warning(
                        f"[RETRY] {fn.__name__} attempt {attempt}/{max_attempts} "
                        f"failed: {exc}. Retrying in {delay:.1f}s ..."
                    )
                    time.sleep(delay)
                    delay *= backoff
        return wrapper
    return decorator


# =============================================================
# BOT
# =============================================================
class TradingBot:
    """
    Single-symbol futures bot.

    Thread model
    ------------
    Main thread       : start-up, blocking event loop
    WebSocket thread  : candle events  ->  _handle_socket()
    Watchdog thread   : detects silent websocket, restarts it
    Daily-maint thread: daily balance reset / halt flag check

    All mutable state is protected by self._lock (RLock so
    methods that hold the lock can safely call each other).
    """

    def __init__(self):
        self._client   = Client(API_KEY, API_SECRET)
        self._lock     = threading.RLock()
        self._shutdown = threading.Event()

        # -- Indicator state ------------------------------------------
        self._ema: float | None        = None
        self._atr: float | None        = None
        self._rsi_gain: float | None   = None
        self._rsi_loss: float | None   = None
        self._prev_close: float | None = None
        self._alpha = 2.0 / (EMA_PERIOD + 1)

        self._vwap_q   = deque(maxlen=VWAP_WINDOW)
        self._vwap_pv  = 0.0
        self._vwap_vol = 0.0

        # -- Position / order state ------------------------------------
        self._position: str | None      = None   # None | "LONG" | "SHORT"
        self._sl_id: int | None         = None
        self._tp_id: int | None         = None
        self._entry_price: float | None = None
        self._entry_qty: float | None   = None

        # -- Daily guard -----------------------------------------------
        self._today                       = date.today()
        self._daily_start_bal: float | None = None
        self._halted: bool                = False

        # -- Exchange filters (loaded once at startup) -----------------
        self._step: float | None         = None
        self._tick: float | None         = None
        self._min_qty: float | None      = None
        self._min_notional: float | None = None

        # -- WebSocket health ------------------------------------------
        self._twm: ThreadedWebsocketManager | None = None
        self._last_candle_ts: float | None         = None

    # ==============================================================
    # ROUNDING
    # ==============================================================
    def _round_qty(self, q: float) -> float:
        q = math.floor(q / self._step) * self._step
        return float(Decimal(str(q)).quantize(Decimal(str(self._step)), ROUND_DOWN))

    def _round_price(self, p: float) -> float:
        return float(Decimal(str(p)).quantize(Decimal(str(self._tick)), ROUND_DOWN))

    # ==============================================================
    # ONE-TIME SETUP
    # ==============================================================
    @retry(max_attempts=3)
    def _load_filters(self):
        info = self._client.futures_exchange_info()
        for sym in info["symbols"]:
            if sym["symbol"] != SYMBOL:
                continue
            fmap = {f["filterType"]: f for f in sym["filters"]}
            self._step         = float(fmap["LOT_SIZE"]["stepSize"])
            self._tick         = float(fmap["PRICE_FILTER"]["tickSize"])
            self._min_qty      = float(fmap["LOT_SIZE"]["minQty"])
            self._min_notional = float(
                fmap.get("MIN_NOTIONAL", {}).get("notional", 5.0)
            )
            log.info(
                f"Filters | step={self._step} tick={self._tick} "
                f"min_qty={self._min_qty} min_notional={self._min_notional}"
            )
            return
        raise RuntimeError(f"Symbol {SYMBOL} not found in exchange info")

    @retry(max_attempts=3)
    def _set_leverage(self):
        self._client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        log.info(f"Leverage set to {LEVERAGE}x")

    # ==============================================================
    # STATE PERSISTENCE  (survive restarts)
    # ==============================================================
    def _save_state(self):
        state = {
            "position":        self._position,
            "sl_id":           self._sl_id,
            "tp_id":           self._tp_id,
            "entry_price":     self._entry_price,
            "entry_qty":       self._entry_qty,
            "today":           str(self._today),
            "daily_start_bal": self._daily_start_bal,
            "halted":          self._halted,
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            log.debug("State persisted.")
        except Exception as exc:
            log.warning(f"Could not save state: {exc}")

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            saved_day = date.fromisoformat(s.get("today", "1970-01-01"))
            if saved_day == date.today():
                self._position        = s.get("position")
                self._sl_id           = s.get("sl_id")
                self._tp_id           = s.get("tp_id")
                self._entry_price     = s.get("entry_price")
                self._entry_qty       = s.get("entry_qty")
                self._daily_start_bal = s.get("daily_start_bal")
                self._halted          = s.get("halted", False)
                log.info(
                    f"State restored | pos={self._position} "
                    f"entry={self._entry_price} halted={self._halted}"
                )
            else:
                log.info("State file is from a previous day - ignoring.")
        except Exception as exc:
            log.warning(f"Could not load state: {exc}")

    # ==============================================================
    # BALANCE
    # ==============================================================
    @retry(max_attempts=3)
    def _fetch_balance(self) -> float:
        return float(self._client.futures_account()["totalWalletBalance"])

    # ==============================================================
    # POSITION RECONCILIATION
    # ==============================================================
    @retry(max_attempts=3)
    def _sync_position(self):
        """
        Compare internal state with Binance.
        Detects SL/TP hits and external position changes.
        Must be called while holding self._lock.
        """
        positions = self._client.futures_position_information(symbol=SYMBOL)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                if self._position != "LONG":
                    log.warning(
                        f"Binance shows LONG {amt} but bot had "
                        f"{self._position}. Correcting."
                    )
                    self._position    = "LONG"
                    self._entry_price = float(p["entryPrice"])
                    self._entry_qty   = abs(amt)
                return
            if amt < 0:
                if self._position != "SHORT":
                    log.warning(
                        f"Binance shows SHORT {amt} but bot had "
                        f"{self._position}. Correcting."
                    )
                    self._position    = "SHORT"
                    self._entry_price = float(p["entryPrice"])
                    self._entry_qty   = abs(amt)
                return

        # Exchange is flat
        if self._position is not None:
            log.info(
                f"SL/TP fired - position closed on exchange. "
                f"Was {self._position}. Resetting."
            )
            self._position    = None
            self._sl_id       = None
            self._tp_id       = None
            self._entry_price = None
            self._entry_qty   = None
            self._save_state()

    # ==============================================================
    # ORDER MANAGEMENT
    # ==============================================================
    @retry(max_attempts=3)
    def _cancel_all_open_orders(self):
        try:
            open_orders = self._client.futures_get_open_orders(symbol=SYMBOL)
            if open_orders:
                self._client.futures_cancel_all_open_orders(symbol=SYMBOL)
                log.info(f"Cancelled {len(open_orders)} open order(s).")
        except BinanceAPIException as exc:
            if exc.code == -2011:   # Unknown order - already gone
                pass
            else:
                raise

    def _calc_qty(self, price: float) -> float | None:
        """Compute trade quantity; returns None if below exchange minimums."""
        balance = self._fetch_balance()
        if balance <= 0:
            log.error("Balance is zero or negative.")
            return None
        qty      = self._round_qty((balance * RISK_PCT * LEVERAGE) / price)
        notional = qty * price
        if qty < self._min_qty:
            log.warning(f"Qty {qty} < min_qty {self._min_qty}. Skipping.")
            return None
        if notional < self._min_notional:
            log.warning(f"Notional {notional:.2f} < min_notional {self._min_notional}. Skipping.")
            return None
        return qty

    def _place_order_atomic(
        self, side: str, qty: float, sl_price: float, tp_price: float
    ) -> bool:
        """
        Atomic order placement:
          1. Cancel stale open orders.
          2. Submit market entry.
          3. Poll until FILLED - use actual executedQty + avgPrice.
          4. Place SL.  Failure -> emergency close.
          5. Place TP.  Failure -> cancel SL + emergency close.
        Returns True on full success.
        """
        opposite = "SELL" if side == "BUY" else "BUY"

        # 1. Clean slate
        self._cancel_all_open_orders()

        # 2. Market entry
        try:
            resp     = self._client.futures_create_order(
                symbol=SYMBOL, side=side, type="MARKET", quantity=qty
            )
            order_id = resp["orderId"]
            log.info(f"MARKET {side} submitted | qty={qty} orderId={order_id}")
        except BinanceAPIException as exc:
            log.error(f"Market order rejected: {exc}")
            return False

        # 3. Wait for fill confirmation
        filled_qty = 0.0
        avg_price  = None
        for _ in range(FILL_POLL_RETRIES):
            time.sleep(FILL_POLL_INTERVAL)
            try:
                o = self._client.futures_get_order(
                    symbol=SYMBOL, orderId=order_id
                )
                if o["status"] == "FILLED":
                    filled_qty = float(o["executedQty"])
                    avg_price  = float(o["avgPrice"])
                    log.info(
                        f"Entry FILLED | avgPrice={avg_price:.4f} "
                        f"qty={filled_qty}"
                    )
                    break
            except Exception as exc:
                log.warning(f"Fill poll error: {exc}")
        else:
            log.critical(
                "Market order NOT confirmed filled within timeout. "
                "SL/TP will NOT be placed. MANUAL ACTION REQUIRED."
            )
            return False

        self._entry_price = avg_price
        self._entry_qty   = filled_qty

        # Recalculate SL/TP from actual fill price (eliminates slippage drift)
        atr_sl = abs(sl_price - avg_price) if side == "BUY" else abs(avg_price - sl_price)
        atr_tp = abs(tp_price - avg_price) if side == "BUY" else abs(avg_price - tp_price)
        if side == "BUY":
            sl_price = avg_price - atr_sl
            tp_price = avg_price + atr_tp
        else:
            sl_price = avg_price + atr_sl
            tp_price = avg_price - atr_tp

        # 4. Stop-Loss
        sl_id = None
        try:
            sl_resp = self._client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="STOP_MARKET",
                stopPrice=self._round_price(sl_price),
                closePosition=True,
                workingType="CONTRACT_PRICE",
            )
            sl_id = sl_resp["orderId"]
            log.info(f"SL placed | price={self._round_price(sl_price)} orderId={sl_id}")
        except BinanceAPIException as exc:
            log.error(f"SL placement FAILED: {exc}. Emergency-closing position.")
            self._emergency_close(opposite, filled_qty)
            return False

        # 5. Take-Profit
        try:
            tp_resp = self._client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="TAKE_PROFIT_MARKET",
                stopPrice=self._round_price(tp_price),
                closePosition=True,
                workingType="CONTRACT_PRICE",
            )
            tp_id = tp_resp["orderId"]
            log.info(f"TP placed | price={self._round_price(tp_price)} orderId={tp_id}")
        except BinanceAPIException as exc:
            log.error(f"TP placement FAILED: {exc}. Cancelling SL + emergency close.")
            try:
                self._client.futures_cancel_order(symbol=SYMBOL, orderId=sl_id)
            except Exception:
                pass
            self._emergency_close(opposite, filled_qty)
            return False

        self._sl_id = sl_id
        self._tp_id = tp_id
        self._save_state()
        return True

    def _emergency_close(self, side: str, qty: float):
        """Market-close a position immediately as a safety fallback."""
        try:
            self._client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True,
            )
            log.info(f"Emergency close executed: {side} {qty}")
        except Exception as exc:
            log.critical(
                f"EMERGENCY CLOSE FAILED: {exc}. "
                "Live position has NO protection. MANUAL ACTION REQUIRED."
            )

    # ==============================================================
    # INDICATOR SEEDING  (full Wilder warm-up, no shortcuts)
    # ==============================================================
    @retry(max_attempts=3)
    def _load_history(self):
        limit = min(max(EMA_PERIOD, RSI_PERIOD, ATR_PERIOD, VWAP_WINDOW) + 150, 500)
        raw   = self._client.futures_klines(
            symbol=SYMBOL, interval=INTERVAL, limit=limit
        )

        closes, highs, lows, vols, typical = [], [], [], [], []
        for k in raw:
            h, l, c, v = float(k[2]), float(k[3]), float(k[4]), float(k[5])
            closes.append(c)
            highs.append(h)
            lows.append(l)
            vols.append(v)
            typical.append((h + l + c) / 3.0)   # correct VWAP typical price

        n = len(closes)

        # EMA - seed = first close, iterate every bar
        ema = closes[0]
        for c in closes:
            ema += self._alpha * (c - ema)
        self._ema = ema

        # ATR - seed = simple avg of first ATR_PERIOD TRs, Wilder-smooth rest
        trs = [
            max(highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]))
            for i in range(1, n)
        ]
        atr = sum(trs[:ATR_PERIOD]) / ATR_PERIOD
        for tr in trs[ATR_PERIOD:]:
            atr = (atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        self._atr = atr

        # RSI - seed first RSI_PERIOD, Wilder-smooth remainder
        gains  = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
        losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
        avg_g  = sum(gains[:RSI_PERIOD])  / RSI_PERIOD
        avg_l  = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for i in range(RSI_PERIOD, len(gains)):
            avg_g = (avg_g * (RSI_PERIOD - 1) + gains[i])  / RSI_PERIOD
            avg_l = (avg_l * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
        self._rsi_gain = avg_g
        self._rsi_loss = avg_l

        # VWAP - rolling window seeded from last VWAP_WINDOW bars
        self._vwap_q.clear()
        self._vwap_pv  = 0.0
        self._vwap_vol = 0.0
        for tp, v in zip(typical[-VWAP_WINDOW:], vols[-VWAP_WINDOW:]):
            pv = tp * v
            self._vwap_q.append((pv, v))
            self._vwap_pv  += pv
            self._vwap_vol += v

        self._prev_close = closes[-1]

        log.info(
            f"History seeded | n={n} EMA={self._ema:.2f} ATR={self._atr:.4f} "
            f"RSI_g={self._rsi_gain:.5f} RSI_l={self._rsi_loss:.5f}"
        )

    # ==============================================================
    # LIVE INDICATOR UPDATE  (called once per closed candle)
    # ==============================================================
    def _update_indicators(
        self, high: float, low: float, close: float, volume: float
    ) -> tuple[float, float, float, float]:
        """
        Must be called while holding self._lock.
        Returns (ema, rsi, atr, vwap).
        """
        typical = (high + low + close) / 3.0

        # EMA
        self._ema += self._alpha * (close - self._ema)

        # ATR (Wilder)
        tr = max(
            high - low,
            abs(high - self._prev_close),
            abs(low  - self._prev_close),
        )
        self._atr = (self._atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD

        # RSI (Wilder)
        change = close - self._prev_close
        self._rsi_gain = (self._rsi_gain * (RSI_PERIOD - 1) + max(change, 0.0))  / RSI_PERIOD
        self._rsi_loss = (self._rsi_loss * (RSI_PERIOD - 1) + max(-change, 0.0)) / RSI_PERIOD
        rs  = self._rsi_gain / self._rsi_loss if self._rsi_loss > 0 else 100.0
        rsi = 100.0 - (100.0 / (1.0 + rs))

        # VWAP (rolling window - evict oldest before deque auto-evicts)
        pv = typical * volume
        if len(self._vwap_q) == VWAP_WINDOW:
            old_pv, old_v   = self._vwap_q[0]   # peek what WILL be evicted
            self._vwap_pv  -= old_pv
            self._vwap_vol -= old_v
        self._vwap_q.append((pv, volume))
        self._vwap_pv  += pv
        self._vwap_vol += volume
        vwap = self._vwap_pv / self._vwap_vol if self._vwap_vol > 0 else close

        self._prev_close = close
        return self._ema, rsi, self._atr, vwap

    # ==============================================================
    # DAILY GUARD
    # ==============================================================
    def _daily_reset_if_needed(self):
        """Call under self._lock."""
        today = date.today()
        if today != self._today:
            self._today           = today
            self._daily_start_bal = self._fetch_balance()
            self._halted          = False
            self._save_state()
            log.info(f"New trading day | start balance={self._daily_start_bal:.2f} USDT")

    def _daily_loss_exceeded(self) -> bool:
        """Returns True (and halts bot) if today's loss is over the limit."""
        if self._halted:
            return True
        if not self._daily_start_bal:
            return False
        current   = self._fetch_balance()
        loss_frac = (self._daily_start_bal - current) / self._daily_start_bal
        if loss_frac >= MAX_DAILY_LOSS_PCT:
            log.error(
                f"Daily loss limit hit ({loss_frac * 100:.2f}% >= "
                f"{MAX_DAILY_LOSS_PCT * 100:.0f}%). Bot halted for today."
            )
            self._halted = True
            self._save_state()
        return self._halted

    # ==============================================================
    # STRATEGY
    # ==============================================================
    def _check_trade(
        self,
        close: float,
        ema: float,
        rsi: float,
        atr: float,
        vwap: float,
    ):
        with self._lock:
            if self._halted:
                log.warning("Bot halted (daily loss). No new entries.")
                return

            # Reconcile - this is where SL/TP hits get detected
            self._sync_position()

            if self._position is not None:
                log.debug(f"In {self._position}. Waiting for exit.")
                return

            # API call only when flat (not on every candle)
            if self._daily_loss_exceeded():
                return

            qty = self._calc_qty(close)
            if qty is None:
                return

            atr_sl = atr * SL_MULT
            atr_tp = atr * TP_MULT

            # LONG: price above EMA (uptrend) + near VWAP (pullback) + RSI not overbought
            if (
                close > ema
                and close < vwap * (1 + VWAP_BAND)
                and rsi < LONG_RSI_MAX
            ):
                sl = close - atr_sl
                tp = close + atr_tp
                log.info(
                    f"LONG SIGNAL | C={close:.2f} EMA={ema:.2f} VWAP={vwap:.2f} "
                    f"RSI={rsi:.1f} ATR={atr:.4f} | SL={sl:.2f} TP={tp:.2f}"
                )
                if self._place_order_atomic("BUY", qty, sl, tp):
                    self._position = "LONG"
                    self._save_state()

            # SHORT: price below EMA (downtrend) + near VWAP (pullback) + RSI not oversold
            elif (
                close < ema
                and close > vwap * (1 - VWAP_BAND)
                and rsi > SHORT_RSI_MIN
            ):
                sl = close + atr_sl
                tp = close - atr_tp
                log.info(
                    f"SHORT SIGNAL | C={close:.2f} EMA={ema:.2f} VWAP={vwap:.2f} "
                    f"RSI={rsi:.1f} ATR={atr:.4f} | SL={sl:.2f} TP={tp:.2f}"
                )
                if self._place_order_atomic("SELL", qty, sl, tp):
                    self._position = "SHORT"
                    self._save_state()

            else:
                log.debug(
                    f"No signal | C={close:.2f} EMA={ema:.2f} "
                    f"VWAP={vwap:.2f} RSI={rsi:.1f}"
                )

    # ==============================================================
    # WEBSOCKET HANDLER
    # ==============================================================
    def _handle_socket(self, msg: dict):
        try:
            if msg.get("e") == "error":
                log.error(f"WebSocket error event: {msg}")
                return
            if msg.get("e") != "kline":
                return

            k = msg["k"]
            if not k["x"]:
                return   # only act on fully closed candles

            self._last_candle_ts = time.time()

            high   = float(k["h"])
            low    = float(k["l"])
            close  = float(k["c"])
            volume = float(k["v"])

            with self._lock:
                ema, rsi, atr, vwap = self._update_indicators(
                    high, low, close, volume
                )

            log.info(
                f"Candle closed | C={close:.2f} EMA={ema:.2f} RSI={rsi:.1f} "
                f"ATR={atr:.4f} VWAP={vwap:.2f} | pos={self._position}"
            )

            self._check_trade(close, ema, rsi, atr, vwap)

        except Exception:
            log.exception("Unhandled error in _handle_socket")

    # ==============================================================
    # WEBSOCKET LIFECYCLE
    # ==============================================================
    def _start_websocket(self):
        self._twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
        self._twm.start()
        self._twm.start_kline_socket(
            callback=self._handle_socket,
            symbol=SYMBOL,
            interval=INTERVAL,
        )
        log.info("WebSocket started.")

    def _restart_websocket(self):
        log.warning("Restarting WebSocket ...")
        try:
            if self._twm:
                self._twm.stop()
        except Exception as exc:
            log.warning(f"Error stopping old TWM: {exc}")
        time.sleep(5)
        self._start_websocket()
        log.info("WebSocket restarted.")

    # ==============================================================
    # BACKGROUND THREADS
    # ==============================================================
    def _watchdog(self):
        """Detect a silent websocket and restart it."""
        log.info("Watchdog thread started.")
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=30)
            if self._shutdown.is_set():
                break
            if self._last_candle_ts is None:
                continue
            elapsed = time.time() - self._last_candle_ts
            if elapsed > WATCHDOG_TIMEOUT_S:
                log.warning(
                    f"No candle for {elapsed:.0f}s "
                    f"(limit={WATCHDOG_TIMEOUT_S}s). Restarting WebSocket."
                )
                self._restart_websocket()
                self._last_candle_ts = time.time()  # prevent immediate re-trigger

    def _daily_maintenance(self):
        """Check for a new calendar day and reset daily state."""
        log.info("Daily maintenance thread started.")
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=60)
            if self._shutdown.is_set():
                break
            try:
                with self._lock:
                    self._daily_reset_if_needed()
            except Exception as exc:
                log.warning(f"Daily maintenance error: {exc}")

    # ==============================================================
    # GRACEFUL SHUTDOWN
    # ==============================================================
    def _on_shutdown(self, signum, _frame):
        log.info(f"Received {signal.Signals(signum).name}. Shutting down ...")
        self._shutdown.set()
        if self._twm:
            try:
                self._twm.stop()
            except Exception:
                pass
        self._save_state()
        log.info("Bot stopped cleanly.")
        sys.exit(0)

    # ==============================================================
    # ENTRY POINT
    # ==============================================================
    def run(self):
        signal.signal(signal.SIGINT,  self._on_shutdown)
        signal.signal(signal.SIGTERM, self._on_shutdown)

        log.info("=" * 60)
        log.info("  LIVE BOT STARTING")
        log.info(f"  Symbol={SYMBOL}  Leverage={LEVERAGE}x")
        log.info("=" * 60)

        # One-time setup
        self._load_filters()
        self._set_leverage()

        # Restore persisted session state
        self._load_state()

        # Seed all indicators from historical klines
        self._load_history()

        # Init daily guard
        balance = self._fetch_balance()
        log.info(f"Wallet balance: {balance:.2f} USDT")
        if self._daily_start_bal is None:
            self._daily_start_bal = balance

        # Reconcile position with exchange (handles restarts mid-trade)
        with self._lock:
            self._sync_position()

        # Cancel any orphan SL/TP orders left from a previous session
        if self._position is None:
            self._cancel_all_open_orders()
        else:
            log.info(
                f"Resuming existing {self._position} @ {self._entry_price}. "
                "No new entries until this position closes."
            )

        # Background threads
        threading.Thread(
            target=self._watchdog, daemon=True, name="watchdog"
        ).start()
        threading.Thread(
            target=self._daily_maintenance, daemon=True, name="daily_maint"
        ).start()

        # Start live feed
        self._start_websocket()
        log.info("Bot is live. Listening for candles ...")

        # Keep main thread alive - everything else is event-driven
        while not self._shutdown.is_set():
            time.sleep(1)


# =============================================================
if __name__ == "__main__":
    TradingBot().run()