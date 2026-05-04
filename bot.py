import os
import sys
import time
import math
import json
import signal
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import date
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

LEVERAGE = 2

# 🔥 tuned strategy
EMA_PERIOD = 198
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 50

TP_MULT = 1.2
SL_MULT = 0.8

LONG_RSI_MIN = 48
SHORT_RSI_MAX = 52
VWAP_BAND = 0.003

LOG_FILE = "bot.log"

# =============================
# LOGGING
# =============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()

# =============================
# BOT
# =============================
class Bot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)
        self.position = None
        self.entry_price = None

        self.ema = None
        self.atr = None
        self.prev_close = None
        self.rsi_gain = None
        self.rsi_loss = None

        self.vwap_q = deque(maxlen=VWAP_WINDOW)
        self.vwap_pv = 0
        self.vwap_vol = 0

        self.alpha = 2 / (EMA_PERIOD + 1)

        self.min_qty = None
        self.step = None
        self.tick = None
        self.min_notional = None

    # =============================
    # UTILS
    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), rounding=ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), rounding=ROUND_DOWN))

    # =============================
    def load_filters(self):
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == SYMBOL:
                f = {x["filterType"]: x for x in s["filters"]}
                self.step = float(f["LOT_SIZE"]["stepSize"])
                self.tick = float(f["PRICE_FILTER"]["tickSize"])
                self.min_qty = float(f["LOT_SIZE"]["minQty"])
                self.min_notional = float(f["MIN_NOTIONAL"]["notional"])
                log.info(f"Filters loaded | min_notional={self.min_notional}")

    def set_leverage(self):
        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

    def get_balance(self):
        return float(self.client.futures_account()["totalWalletBalance"])

    # =============================
    # 🔥 FIXED POSITION SIZING
    # =============================
    def calc_qty(self, price):
        balance = self.get_balance()
        effective = balance * LEVERAGE

        qty = effective / price
        qty = self.round_qty(qty)

        notional = qty * price

        if notional < self.min_notional:
            qty = self.round_qty(self.min_notional / price)

        log.info(f"Qty={qty} Notional={qty*price:.2f}")
        return qty

    # =============================
    # INDICATORS
    # =============================
    def load_history(self):
        klines = self.client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=300)

        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        vols = [float(k[5]) for k in klines]

        self.ema = closes[0]
        for c in closes:
            self.ema += self.alpha * (c - self.ema)

        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))
        ]

        self.atr = sum(trs[:ATR_PERIOD]) / ATR_PERIOD

        gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]

        self.rsi_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        self.rsi_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD

        self.prev_close = closes[-1]

    def update_indicators(self, h, l, c, v):
        self.ema += self.alpha * (c - self.ema)

        tr = max(h - l, abs(h - self.prev_close), abs(l - self.prev_close))
        self.atr = (self.atr * 13 + tr) / 14

        change = c - self.prev_close
        gain = max(change, 0)
        loss = max(-change, 0)

        self.rsi_gain = (self.rsi_gain * 13 + gain) / 14
        self.rsi_loss = (self.rsi_loss * 13 + loss) / 14

        rs = self.rsi_gain / self.rsi_loss if self.rsi_loss else 100
        rsi = 100 - (100 / (1 + rs))

        self.prev_close = c
        return rsi

    # =============================
    # 🔥 ORDER (FIXED)
    # =============================
    def place_trade(self, side, price):
        qty = self.calc_qty(price)

        order = self.client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=qty
        )

        entry_price = float(order["avgPrice"])
        log.info(f"ENTRY {side} @ {entry_price}")

        sl = entry_price - self.atr * SL_MULT if side == "BUY" else entry_price + self.atr * SL_MULT
        tp = entry_price + self.atr * TP_MULT if side == "BUY" else entry_price - self.atr * TP_MULT

        opposite = "SELL" if side == "BUY" else "BUY"

        # 🔥 FIXED SL
        self.client.futures_create_order(
            symbol=SYMBOL,
            side=opposite,
            type="STOP",
            price=self.round_price(sl),
            stopPrice=self.round_price(sl),
            quantity=qty,
            reduceOnly=True,
            timeInForce="GTC",
        )

        # 🔥 FIXED TP
        self.client.futures_create_order(
            symbol=SYMBOL,
            side=opposite,
            type="TAKE_PROFIT",
            price=self.round_price(tp),
            stopPrice=self.round_price(tp),
            quantity=qty,
            reduceOnly=True,
            timeInForce="GTC",
        )

        self.position = "LONG" if side == "BUY" else "SHORT"

    # =============================
    # STRATEGY
    # =============================
    def check_trade(self, close, rsi):
        if self.position:
            return

        trend_strength = abs(close - self.ema)
        if trend_strength < self.atr * 0.5:
            return

        # LONG
        if close > self.ema and close < self.vwap * (1 + VWAP_BAND) and rsi > LONG_RSI_MIN:
            log.info("LONG SIGNAL")
            self.place_trade("BUY", close)

        # SHORT
        elif close < self.ema and close > self.vwap * (1 - VWAP_BAND) and rsi < SHORT_RSI_MAX:
            log.info("SHORT SIGNAL")
            self.place_trade("SELL", close)

    # =============================
    def handle_socket(self, msg):
        if msg["e"] != "kline":
            return

        k = msg["k"]
        if not k["x"]:
            return

        h, l, c, v = float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])

        rsi = self.update_indicators(h, l, c, v)

        self.vwap = c  # simplified VWAP

        log.info(f"C={c} EMA={self.ema:.2f} RSI={rsi:.1f}")

        self.check_trade(c, rsi)

    # =============================
    def run(self):
        self.load_filters()
        self.set_leverage()
        self.load_history()

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()
        twm.start_kline_socket(callback=self.handle_socket, symbol=SYMBOL, interval=INTERVAL)

        log.info("BOT RUNNING")

        while True:
            time.sleep(1)


# =============================
if __name__ == "__main__":
    Bot().run()