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
EMA_PERIOD = 198
ATR_PERIOD = 14
RSI_PERIOD = 14
COOLDOWN = 2

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
        self.gain = 0
        self.loss = 0

        self.vwap_pv = 0
        self.vwap_vol = 0

        self.cooldown = 0
        self.alpha = 2 / (EMA_PERIOD + 1)

        self.csv_file = f"trades_{SYMBOL}.csv"

        try:
            with open(self.csv_file, "x", newline="") as f:
                csv.writer(f).writerow(["time","side","entry","tp","sl","qty"])
        except:
            pass

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
        try:
            return float(self.client.futures_account().get("availableBalance", 0))
        except:
            return 0

    def get_position(self):
        try:
            pos = self.client.futures_position_information(symbol=SYMBOL)
            return float(pos[0].get("positionAmt", 0)) if pos else 0
        except:
            return 0

    # =============================
    def update_indicators(self, close, high, low, volume):

        # EMA
        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None, None, None

        self.ema += self.alpha * (close - self.ema)

        # ATR
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.tr_queue.append(tr)
        if len(self.tr_queue) == ATR_PERIOD:
            self.atr = sum(self.tr_queue) / ATR_PERIOD

        # RSI
        change = close - self.prev_close
        self.rsi_queue.append(change)

        if len(self.rsi_queue) == RSI_PERIOD:
            gains = sum(x for x in self.rsi_queue if x > 0)
            losses = abs(sum(x for x in self.rsi_queue if x < 0))
            if losses == 0:
                rsi = 100
            else:
                rs = gains / losses
                rsi = 100 - (100 / (1 + rs))
        else:
            rsi = None

        # VWAP
        self.vwap_pv += close * volume
        self.vwap_vol += volume
        vwap = self.vwap_pv / self.vwap_vol if self.vwap_vol > 0 else None

        self.prev_close = close

        return self.ema, self.atr, rsi, vwap

    # =============================
    def place_trade(self, side, price, atr):

        if self.cooldown > 0 or self.get_position() != 0:
            return

        qty = self.round_qty((self.get_balance() * LEVERAGE * 0.9) / price)
        if qty * price < self.min_notional:
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            self.client.futures_create_order(
                symbol=SYMBOL, side=side, type="MARKET", quantity=qty
            )

            time.sleep(0.5)

            entry = float(self.client.futures_position_information(symbol=SYMBOL)[0]["entryPrice"])

            sl = entry - atr if side == "BUY" else entry + atr
            tp = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5

            sl = self.round_price(sl)
            tp = self.round_price(tp)

            log.info(f"{side} | Entry={entry:.2f} TP={tp:.2f} SL={sl:.2f}")

            self.client.futures_create_order(
                symbol=SYMBOL, side=opposite, type="STOP_MARKET",
                stopPrice=sl, reduceOnly=True, closePosition=True
            )

            self.client.futures_create_order(
                symbol=SYMBOL, side=opposite, type="TAKE_PROFIT_MARKET",
                stopPrice=tp, reduceOnly=True, closePosition=True
            )

            self.cooldown = COOLDOWN

        except Exception as e:
            log.error(f"TRADE ERROR: {e}")

    # =============================
    def strategy(self, close, ema, atr, rsi, vwap):

        if None in (ema, atr, rsi, vwap):
            return

        # YOUR ORIGINAL STRATEGY (UNCHANGED)
        if close > ema and close > vwap and rsi > 50:
            self.place_trade("BUY", close, atr)

        elif close < ema and close < vwap and rsi < 50:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):
        try:
            if not isinstance(msg, dict):
                return

            if msg.get("e") != "kline":
                return

            k = msg.get("k")
            if not k or not k.get("x"):
                return

            close = float(k["c"])
            high = float(k["h"])
            low = float(k["l"])
            volume = float(k["v"])

            ema, atr, rsi, vwap = self.update_indicators(close, high, low, volume)

            self.strategy(close, ema, atr, rsi, vwap)

            if self.cooldown > 0:
                self.cooldown -= 1

        except Exception as e:
            log.error(f"WS ERROR: {e} | MSG: {msg}")

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


if __name__ == "__main__":
    ProBot().run()