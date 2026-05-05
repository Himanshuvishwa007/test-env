import os
import time
import csv
import logging
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from collections import deque

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

EMA_PERIOD = 50
ATR_PERIOD = 14

VWAP_WINDOW = 50
VOL_WINDOW = 20

COOLDOWN = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger()

# =============================
class ProBot:

    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)

        self.ema = None
        self.prev_close = None

        self.rsi_gain = None
        self.rsi_loss = None

        self.atr = None
        self.tr_queue = deque(maxlen=ATR_PERIOD)

        self.vwap_queue = deque(maxlen=VWAP_WINDOW)
        self.vwap_pv = 0
        self.vwap_vol = 0

        self.vol_queue = deque(maxlen=VOL_WINDOW)

        self.cooldown = 0
        self.alpha = 2 / (EMA_PERIOD + 1)

        self.csv_file = f"trades_{SYMBOL}.csv"

        try:
            with open(self.csv_file, "x", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "side", "entry", "tp", "sl", "qty"])
        except FileExistsError:
            pass

        # =============================
        # Exchange precision
        # =============================
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
    def log_trade(self, side, entry, tp, sl, qty):
        try:
            with open(self.csv_file, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    side,
                    entry,
                    tp,
                    sl,
                    qty
                ])
        except Exception as e:
            log.error(f"CSV log error: {e}")

    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    # =============================
    def get_balance(self):
        try:
            return float(self.client.futures_account()["availableBalance"])
        except Exception as e:
            log.error(f"Balance error: {e}")
            return 0

    def get_position(self):
        try:
            positions = self.client.futures_position_information(symbol=SYMBOL)
            for pos in positions:
                if pos["symbol"] == SYMBOL:
                    return float(pos["positionAmt"])
            return 0
        except Exception as e:
            log.error(f"Position error: {e}")
            return 0

    # =============================
    def load_history(self):
        klines = self.client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=150)

        for k in klines:
            self.update_indicators(
                float(k[4]),
                float(k[2]),
                float(k[3]),
                float(k[5])
            )

        log.info("Indicators ready")

    # =============================
    def update_indicators(self, close, high, low, volume):

        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None, None, None, None

        # EMA
        self.ema += self.alpha * (close - self.ema)

        # RSI
        change = close - self.prev_close
        gain = max(change, 0)
        loss = max(-change, 0)

        if self.rsi_gain is None:
            self.rsi_gain = gain
            self.rsi_loss = loss
        else:
            self.rsi_gain = (self.rsi_gain * 13 + gain) / 14
            self.rsi_loss = (self.rsi_loss * 13 + loss) / 14

        rsi = 100 if self.rsi_loss == 0 else 100 - (100 / (1 + self.rsi_gain / self.rsi_loss))

        # ATR
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.tr_queue.append(tr)

        if len(self.tr_queue) == ATR_PERIOD:
            self.atr = sum(self.tr_queue) / ATR_PERIOD

        # VWAP
        typical = (high + low + close) / 3
        pv = typical * volume

        if len(self.vwap_queue) == VWAP_WINDOW:
            old_pv, old_v = self.vwap_queue[0]
            self.vwap_pv -= old_pv
            self.vwap_vol -= old_v

        self.vwap_queue.append((pv, volume))
        self.vwap_pv += pv
        self.vwap_vol += volume

        vwap = self.vwap_pv / self.vwap_vol if self.vwap_vol > 0 else close

        # Volume
        self.vol_queue.append(volume)
        avg_vol = sum(self.vol_queue) / len(self.vol_queue)
        volume_spike = volume > avg_vol * 1.5

        self.prev_close = close

        return self.ema, rsi, self.atr, vwap, volume_spike

    # =============================
    def get_qty(self, price):
        balance = self.get_balance()

        if balance < 5:
            log.warning(f"Too low balance: {balance}")
            return None

        usable_balance = balance * 0.95
        notional = usable_balance * LEVERAGE

        qty = self.round_qty(notional / price)

        if qty * price < self.min_notional:
            log.warning(f"Below min notional | Qty={qty} Value={qty*price}")
            return None

        return qty

    # =============================
    def place_trade(self, side, price, atr):

        if self.cooldown > 0:
            return

        if self.get_position() != 0:
            log.info("Already in position")
            return

        qty = self.get_qty(price)
        if qty is None:
            log.info("Trade skipped (low balance or min notional)")
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            order = self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            time.sleep(0.5)

            entry = float(order["avgPrice"]) if "avgPrice" in order else price

            sl = entry - atr if side == "BUY" else entry + atr
            tp = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5

            sl = self.round_price(sl)
            tp = self.round_price(tp)

            log.info(f"{side} EXECUTED | Entry={entry} | TP={tp} SL={sl} | QTY={qty}")

            self.log_trade(side, entry, tp, sl, qty)

            # SL
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="STOP_MARKET",
                stopPrice=sl,
                closePosition=True,
                workingType="MARK_PRICE"
            )

            # TP
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                closePosition=True,
                workingType="MARK_PRICE"
            )

            self.cooldown = COOLDOWN

        except Exception as e:
            if hasattr(e, 'code'):
                log.error(f"Binance Error {e.code}: {e.message}")
            else:
                log.error(f"Trade error: {e}")

    # =============================
    def strategy(self, close, ema, rsi, atr, vwap, volume_spike):

        if ema is None or atr is None:
            return

        near_vwap = abs(close - vwap) / vwap < 0.003

        log.info(f"C={close:.2f} EMA={ema:.2f} RSI={rsi:.1f} VWAP={vwap:.2f}")

        # Slightly relaxed condition
        if close > ema and 40 < rsi < 60 and near_vwap:
            self.place_trade("BUY", close, atr)

        elif close < ema and 40 < rsi < 60 and near_vwap:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):
        try:
            if msg["e"] != "kline":
                return

            k = msg["k"]
            if not k["x"]:
                return

            ema, rsi, atr, vwap, volume_spike = self.update_indicators(
                float(k["c"]),
                float(k["h"]),
                float(k["l"]),
                float(k["v"])
            )

            self.strategy(float(k["c"]), ema, rsi, atr, vwap, volume_spike)

            if self.cooldown > 0:
                self.cooldown -= 1

        except Exception as e:
            log.error(f"WS ERROR: {e}")

    # =============================
    def run(self):

        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        self.load_history()

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()

        twm.start_kline_socket(
            callback=self.on_message,
            symbol=SYMBOL,
            interval=INTERVAL
        )

        log.info("🚀 BOT RUNNING")

        while True:
            time.sleep(1)


# =============================
if __name__ == "__main__":
    ProBot().run()