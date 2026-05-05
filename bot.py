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

COOLDOWN = 2

# =============================
# LOGGING (CLEAN)
# =============================
log = logging.getLogger()

log.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(message)s")

# Console (minimal)
console = logging.StreamHandler()
console.setFormatter(formatter)
console.setLevel(logging.INFO)

# File (important logs only)
file_handler = logging.FileHandler("bot.log")
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

log.addHandler(console)
log.addHandler(file_handler)

# =============================
class ProBot:

    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)

        self.ema = None
        self.prev_close = None

        self.atr = None
        self.tr_queue = deque(maxlen=ATR_PERIOD)

        self.cooldown = 0
        self.alpha = 2 / (EMA_PERIOD + 1)

        self.csv_file = f"trades_{SYMBOL}.csv"

        try:
            with open(self.csv_file, "x", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["time", "side", "entry", "tp", "sl", "qty"])
        except FileExistsError:
            pass

        # Precision
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
        with open(self.csv_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                side, entry, tp, sl, qty
            ])

    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    def get_balance(self):
        return float(self.client.futures_account()["availableBalance"])

    def get_position(self):
        pos = self.client.futures_position_information(symbol=SYMBOL)
        return float(pos[0]["positionAmt"])

    # =============================
    def update_indicators(self, close, high, low):

        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None

        # EMA
        self.ema += self.alpha * (close - self.ema)

        # ATR
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.tr_queue.append(tr)

        if len(self.tr_queue) == ATR_PERIOD:
            self.atr = sum(self.tr_queue) / ATR_PERIOD

        self.prev_close = close

        return self.ema, self.atr

    # =============================
    def get_qty(self, price):
        balance = self.get_balance()

        if balance < 5:
            return None

        notional = balance * LEVERAGE * 0.9
        qty = self.round_qty(notional / price)

        if qty * price < self.min_notional:
            return None

        return qty

    # =============================
    def place_trade(self, side, price, atr):

        if self.cooldown > 0:
            return

        if self.get_position() != 0:
            return

        if price <= 0 or atr is None:
            return

        qty = self.get_qty(price)
        if qty is None:
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            # MARKET ENTRY
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            time.sleep(0.5)

            # ✅ REAL ENTRY PRICE FIX
            pos_info = self.client.futures_position_information(symbol=SYMBOL)[0]
            entry = float(pos_info['entryPrice'])

            # SL / TP
            sl = entry - atr if side == "BUY" else entry + atr
            tp = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5

            sl = self.round_price(sl)
            tp = self.round_price(tp)

            log.info(f"{side} | Entry={entry:.2f} TP={tp:.2f} SL={sl:.2f} Qty={qty}")

            self.log_trade(side, entry, tp, sl, qty)

            # STOP LOSS
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="STOP_MARKET",
                stopPrice=sl,
                reduceOnly=True,
                closePosition=True,
                workingType="MARK_PRICE"
            )

            # TAKE PROFIT
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                reduceOnly=True,
                closePosition=True,
                workingType="MARK_PRICE"
            )

            self.cooldown = COOLDOWN

        except Exception as e:
            log.error(f"TRADE ERROR: {e}")

    # =============================
    def strategy(self, close, ema, atr):

        if ema is None or atr is None:
            return

        if close > ema:
            self.place_trade("BUY", close, atr)

        elif close < ema:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):
        try:
            if msg["e"] != "kline":
                return

            k = msg["k"]
            if not k["x"]:
                return

            close = float(k["c"])
            high = float(k["h"])
            low = float(k["l"])

            ema, atr = self.update_indicators(close, high, low)

            self.strategy(close, ema, atr)

            if self.cooldown > 0:
                self.cooldown -= 1

        except Exception as e:
            log.error(f"WS ERROR: {e}")

    # =============================
    def run(self):

        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()

        twm.start_kline_socket(
            callback=self.on_message,
            symbol=SYMBOL,
            interval=INTERVAL
        )

        log.info("🚀 BOT STARTED")

        while True:
            time.sleep(1)


# =============================
if __name__ == "__main__":
    ProBot().run()