import os
import time
import math
import logging
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

LEVERAGE = 2
RISK_PCT = 1.0

EMA_PERIOD = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

COOLDOWN = 2   # candles to wait after trade

# =============================
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

        self.cooldown = 0
        self.position_mode = "ONEWAY"

        self.step = 0.001
        self.tick = 0.01
        self.min_notional = 20

        self.alpha = 2 / (EMA_PERIOD + 1)

        # stats
        self.trades = 0

    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    def get_balance(self):
        return float(self.client.futures_account()["totalWalletBalance"])

    # =============================
    def load_history(self):
        klines = self.client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=100)

        for k in klines:
            close = float(k[4])
            high = float(k[2])
            low = float(k[3])
            self.update_indicators(close, high, low)

        log.info("Indicators ready")

    # =============================
    def update_indicators(self, close, high, low):

        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None, None

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

        self.prev_close = close

        return self.ema, rsi, self.atr

    # =============================
    def get_qty(self, price):
        balance = self.get_balance()
        effective = balance * LEVERAGE * RISK_PCT

        qty = self.round_qty(effective / price)
        notional = qty * price

        if notional < self.min_notional:
            return None

        return qty

    # =============================
    def get_entry_price(self, order_id):
        for _ in range(10):
            time.sleep(0.3)
            o = self.client.futures_get_order(symbol=SYMBOL, orderId=order_id)
            if o["status"] == "FILLED":
                return float(o["avgPrice"])
        return None

    # =============================
    def place_trade(self, side, price, atr):

        if self.cooldown > 0:
            log.info("Cooldown active")
            return

        qty = self.get_qty(price)
        if qty is None:
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            order = self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            entry = self.get_entry_price(order["orderId"])
            if not entry:
                return

            # ATR based SL/TP
            sl = entry - atr if side == "BUY" else entry + atr
            tp = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5

            params = dict(
                symbol=SYMBOL,
                side=opposite,
                workingType="MARK_PRICE",
                closePosition=True
            )

            # SL
            self.client.futures_create_order(
                **params,
                type="STOP_MARKET",
                stopPrice=self.round_price(sl)
            )

            # TP
            self.client.futures_create_order(
                **params,
                type="TAKE_PROFIT_MARKET",
                stopPrice=self.round_price(tp)
            )

            self.trades += 1
            self.cooldown = COOLDOWN

            log.info(f"TRADE #{self.trades} {side} @ {entry}")

        except Exception as e:
            log.error(e)

    # =============================
    def strategy(self, close, ema, rsi, atr):

        if ema is None or atr is None:
            return

        log.info(f"C={close:.2f} EMA={ema:.2f} RSI={rsi:.1f} ATR={atr:.2f}")

        # HIGH QUALITY FILTERS

        # LONG
        if close > ema and 45 < rsi < 55:
            self.place_trade("BUY", close, atr)

        # SHORT
        elif close < ema and 45 < rsi < 55:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):

        if msg["e"] != "kline":
            return

        k = msg["k"]
        if not k["x"]:
            return

        close = float(k["c"])
        high = float(k["h"])
        low = float(k["l"])

        ema, rsi, atr = self.update_indicators(close, high, low)

        self.strategy(close, ema, rsi, atr)

        if self.cooldown > 0:
            self.cooldown -= 1

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

        log.info("PRO BOT RUNNING")

        while True:
            time.sleep(1)


# =============================
if __name__ == "__main__":
    ProBot().run()