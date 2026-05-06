import os
import time
import hmac
import hashlib
import logging
import requests
import threading
from decimal import Decimal, ROUND_DOWN
from collections import deque
from urllib.parse import urlencode

from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager

# =============================
# CONFIG
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

LEVERAGE = 3
EMA_PERIOD = 198
ATR_PERIOD = 15
RSI_PERIOD = 15
VWAP_PERIOD = 288
COOLDOWN = 2

BASE_URL = "https://fapi.binance.com"

# =============================
# LOGGING
# =============================
log = logging.getLogger()
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(message)s")
console = logging.StreamHandler()
console.setFormatter(formatter)
log.addHandler(console)
file_handler = logging.FileHandler("bot.log")
file_handler.setFormatter(formatter)
log.addHandler(file_handler)


# =============================
class ProBot:

    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)

        self.ema = None
        self.prev_close = None
        self.atr = None
        self.tr_queue = deque(maxlen=ATR_PERIOD)
        self.rsi_queue = deque(maxlen=RSI_PERIOD)
        self.vwap_queue = deque(maxlen=VWAP_PERIOD)
        self.vwap_pv_sum = 0
        self.vwap_vol_sum = 0
        self.cooldown = 0
        self.alpha = 2 / (EMA_PERIOD + 1)

        # Track open algo order IDs so we can cancel the orphan
        self.sl_algo_id = None
        self.tp_algo_id = None

        # Internal position state — updated by monitor thread
        # True means we believe we are IN a trade right now
        self.in_trade = False
        self._trade_lock = threading.Lock()

        # precision
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == SYMBOL:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self.step = float(f["stepSize"])
                    if f["filterType"] == "PRICE_FILTER":
                        self.tick = float(f["tickSize"])
                    if f["filterType"] == "MIN_NOTIONAL":
                        self.min_notional = float(f["notional"])
                break

    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    def get_balance(self):
        return float(self.client.futures_account()["availableBalance"])

    def get_position(self):
        """Returns position amount. Positive=LONG, Negative=SHORT, 0=flat."""
        try:
            pos = self.client.futures_position_information(symbol=SYMBOL)
            for p in pos:
                amt = float(p["positionAmt"])
                if amt != 0:
                    return amt
        except Exception as e:
            log.warning(f"get_position error: {e}")
        return 0.0

    # =============================
    def _sign_request(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        return params

    def place_algo_order(self, side, order_type, trigger_price, qty):
        """POST to /fapi/v1/algoOrder. Returns algoId."""
        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": order_type,
            "algoType": "CONDITIONAL",
            "quantity": str(qty),
            "triggerPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "timeInForce": "GTC",
        }
        params = self._sign_request(params)
        resp = requests.post(
            f"{BASE_URL}/fapi/v1/algoOrder",
            headers={"X-MBX-APIKEY": API_KEY},
            params=params,
            timeout=10
        )
        data = resp.json()
        if resp.status_code != 200 or "algoId" not in data:
            raise Exception(f"Algo order failed: {data}")
        return data["algoId"]

    def cancel_algo_order(self, algo_id):
        """Cancel a specific algo order by ID."""
        try:
            params = self._sign_request({"symbol": SYMBOL, "algoId": algo_id})
            resp = requests.delete(
                f"{BASE_URL}/fapi/v1/algoOrder",
                headers={"X-MBX-APIKEY": API_KEY},
                params=params,
                timeout=10
            )
            data = resp.json()
            log.info(f"Cancelled algo order {algo_id}: {data.get('algoStatus', data)}")
        except Exception as e:
            log.warning(f"Cancel algo order {algo_id} error: {e}")

    def cancel_all_algo_orders(self):
        """Cancel both tracked SL and TP algo orders."""
        if self.sl_algo_id:
            self.cancel_algo_order(self.sl_algo_id)
            self.sl_algo_id = None
        if self.tp_algo_id:
            self.cancel_algo_order(self.tp_algo_id)
            self.tp_algo_id = None

    # =============================
    def position_monitor(self):
        """
        Background thread — polls position every 3s.
        When position goes flat after being in a trade:
        - cancels the orphan algo order (the one that didn't trigger)
        - resets in_trade flag so bot can take next signal
        """
        log.info("🔍 Position monitor started")
        while True:
            try:
                amt = self.get_position()
                with self._trade_lock:
                    if self.in_trade and amt == 0:
                        log.info("✅ Position closed — cancelling orphan algo orders")
                        self.cancel_all_algo_orders()
                        self.in_trade = False
            except Exception as e:
                log.warning(f"Monitor error: {e}")
            time.sleep(3)

    # =============================
    def update_indicators(self, close, high, low, volume):

        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None, None, None

        self.ema += self.alpha * (close - self.ema)

        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.tr_queue.append(tr)
        if len(self.tr_queue) == ATR_PERIOD:
            self.atr = sum(self.tr_queue) / ATR_PERIOD

        change = close - self.prev_close
        self.rsi_queue.append(change)
        if len(self.rsi_queue) == RSI_PERIOD:
            gains = sum(x for x in self.rsi_queue if x > 0)
            losses = abs(sum(x for x in self.rsi_queue if x < 0))
            rsi = 100 if losses == 0 else 100 - (100 / (1 + gains / losses))
        else:
            rsi = None

        pv = close * volume
        if len(self.vwap_queue) == self.vwap_queue.maxlen:
            old_pv, old_vol = self.vwap_queue[0]
            self.vwap_pv_sum -= old_pv
            self.vwap_vol_sum -= old_vol
        self.vwap_queue.append((pv, volume))
        self.vwap_pv_sum += pv
        self.vwap_vol_sum += volume
        vwap = self.vwap_pv_sum / self.vwap_vol_sum if self.vwap_vol_sum > 0 else None

        self.prev_close = close
        return self.ema, self.atr, rsi, vwap

    # =============================
    def preload_data(self):
        log.info("📊 Preloading VWAP (288 candles)...")
        klines = self.client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=VWAP_PERIOD)
        for k in klines:
            self.update_indicators(float(k[4]), float(k[2]), float(k[3]), float(k[5]))
        log.info("✅ Preload complete")

    # =============================
    def place_trade(self, side, price, atr):

        with self._trade_lock:
            # Block if already in a trade — monitor thread is the only thing that clears this
            if self.in_trade:
                return
            if self.cooldown > 0:
                return

        if atr is None or atr <= 0:
            return

        # Double-check actual position from exchange (safety net)
        if self.get_position() != 0:
            with self._trade_lock:
                self.in_trade = True  # sync state
            return

        balance = self.get_balance()
        if balance < 5:
            return

        qty = self.round_qty((balance * LEVERAGE * 0.9) / price)
        if qty * price < self.min_notional:
            return

        close_side = "SELL" if side == "BUY" else "BUY"

        try:
            # ── ENTRY ───────────────────────────────────────────
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            time.sleep(0.5)

            # Fetch actual fill price
            pos = self.client.futures_position_information(symbol=SYMBOL)
            entry = None
            for p in pos:
                if float(p["positionAmt"]) != 0:
                    entry = float(p["entryPrice"])
                    break

            if not entry or entry == 0:
                log.error("Could not fetch entry price — aborting SL/TP")
                return

            sl = self.round_price(entry - atr if side == "BUY" else entry + atr)
            tp = self.round_price(entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5)

            log.info(f"{side} | Entry={entry:.2f} | TP={tp:.2f} | SL={sl:.2f} | Qty={qty}")

            # ── STOP LOSS via Algo endpoint ─────────────────────
            self.sl_algo_id = self.place_algo_order(close_side, "STOP_MARKET", sl, qty)
            log.info(f"SL algo order placed: ID={self.sl_algo_id}")

            # ── TAKE PROFIT via Algo endpoint ───────────────────
            self.tp_algo_id = self.place_algo_order(close_side, "TAKE_PROFIT_MARKET", tp, qty)
            log.info(f"TP algo order placed: ID={self.tp_algo_id}")

            with self._trade_lock:
                self.in_trade = True
                self.cooldown = COOLDOWN

        except Exception as e:
            log.error(f"TRADE ERROR: {e}")
            self.cancel_all_algo_orders()

    # =============================
    def strategy(self, close, ema, atr, rsi, vwap):
        if None in (ema, atr, rsi, vwap):
            return

        # Mean reversion — same as paper strategy
        # LONG: price above EMA, slightly below VWAP (within 0.2%), RSI weak
        if close > ema and close < vwap * 1.002 and rsi < 45:
            self.place_trade("BUY", close, atr)

        # SHORT: price below EMA, slightly above VWAP (within 0.2%), RSI strong
        elif close < ema and close > vwap * 0.998 and rsi > 55:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):
        try:
            if msg.get("e") != "kline":
                return
            k = msg["k"]
            if not k["x"]:
                return

            close = float(k["c"])
            high = float(k["h"])
            low = float(k["l"])
            volume = float(k["v"])

            ema, atr, rsi, vwap = self.update_indicators(close, high, low, volume)

            log.info(
                f"CLOSE={close:.2f} | EMA={round(ema,2) if ema else 'NA'} | "
                f"RSI={round(rsi,2) if rsi else 'NA'} | "
                f"VWAP={round(vwap,2) if vwap else 'NA'} | "
                f"ATR={round(atr,2) if atr else 'NA'} | "
                f"IN_TRADE={self.in_trade}"
            )

            self.strategy(close, ema, atr, rsi, vwap)

            if self.cooldown > 0:
                self.cooldown -= 1

        except Exception as e:
            log.error(f"WS ERROR: {e}")

    # =============================
    def run(self):
        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        self.preload_data()

        # Start position monitor in background
        monitor = threading.Thread(target=self.position_monitor, daemon=True)
        monitor.start()

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()
        twm.start_kline_socket(callback=self.on_message, symbol=SYMBOL, interval=INTERVAL)

        log.info("🚀 BOT STARTED")
        while True:
            time.sleep(1)


if __name__ == "__main__":
    ProBot().run()