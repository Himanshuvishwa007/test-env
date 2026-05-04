import os
import time
import math
import logging
from decimal import Decimal, ROUND_DOWN

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
RISK_PCT = 1.0   # use full balance for small account

EMA_PERIOD = 50
RSI_PERIOD = 14

# =============================
# LOGGING
# =============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger()

# =============================
# BOT
# =============================
class Bot:

    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)

        self.ema = None
        self.prev_close = None

        self.rsi_gain = None
        self.rsi_loss = None

        self.position = None
        self.position_mode = "BOTH"  # default

        self.step = 0.001
        self.tick = 0.01
        self.min_notional = 20

        self.alpha = 2 / (EMA_PERIOD + 1)

    # =============================
    # HELPERS
    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    def get_balance(self):
        acc = self.client.futures_account()
        return float(acc["totalWalletBalance"])

    def detect_mode(self):
        try:
            info = self.client.futures_get_position_mode()
            if info["dualSidePosition"]:
                log.info("HEDGE MODE detected")
                return "HEDGE"
        except:
            pass
        log.info("ONE-WAY MODE detected")
        return "ONEWAY"

    # =============================
    # INDICATORS
    # =============================
    def update_indicators(self, close):
        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None

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

        rs = self.rsi_gain / self.rsi_loss if self.rsi_loss != 0 else 0
        rsi = 100 - (100 / (1 + rs))

        self.prev_close = close

        return self.ema, rsi

    # =============================
    # POSITION SIZE
    # =============================
    def get_qty(self, price):
        balance = self.get_balance()
        effective = balance * LEVERAGE * RISK_PCT

        qty = self.round_qty(effective / price)
        notional = qty * price

        log.info(f"Balance={balance:.2f} Qty={qty} Notional={notional:.2f}")

        if notional < self.min_notional:
            log.warning("Below min notional")
            return None

        return qty

    # =============================
    # TRADE
    # =============================
    def place_trade(self, side, price):

        qty = self.get_qty(price)
        if qty is None:
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            # ENTRY
            order = self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            entry_price = float(order["avgPrice"])
            log.info(f"ENTRY {side} @ {entry_price}")

            # SL / TP
            sl = entry_price * (0.998 if side == "BUY" else 1.002)
            tp = entry_price * (1.002 if side == "BUY" else 0.998)

            params = dict(
                symbol=SYMBOL,
                side=opposite,
                workingType="MARK_PRICE",
                closePosition=True
            )

            # hedge mode fix
            if self.position_mode == "HEDGE":
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"

            # STOP LOSS
            self.client.futures_create_order(
                **params,
                type="STOP_MARKET",
                stopPrice=self.round_price(sl)
            )

            # TAKE PROFIT
            self.client.futures_create_order(
                **params,
                type="TAKE_PROFIT_MARKET",
                stopPrice=self.round_price(tp)
            )

            log.info("SL & TP placed successfully")

        except Exception as e:
            log.error(f"Trade error: {e}")

            # emergency close
            try:
                self.client.futures_create_order(
                    symbol=SYMBOL,
                    side=opposite,
                    type="MARKET",
                    quantity=qty,
                    reduceOnly=True
                )
                log.info("Emergency close executed")
            except:
                log.error("Emergency close failed")

    # =============================
    # STRATEGY
    # =============================
    def check_trade(self, close, ema, rsi):

        if ema is None:
            return

        log.info(f"C={close} EMA={ema:.2f} RSI={rsi:.2f}")

        # LONG
        if close > ema and rsi < 50:
            self.place_trade("BUY", close)

        # SHORT
        elif close < ema and rsi > 50:
            self.place_trade("SELL", close)

    # =============================
    # SOCKET
    # =============================
    def on_message(self, msg):

        if msg["e"] != "kline":
            return

        k = msg["k"]

        if not k["x"]:
            return

        close = float(k["c"])

        ema, rsi = self.update_indicators(close)
        self.check_trade(close, ema, rsi)

    # =============================
    # RUN
    # =============================
    def run(self):

        self.position_mode = self.detect_mode()

        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()

        twm.start_kline_socket(
            callback=self.on_message,
            symbol=SYMBOL,
            interval=INTERVAL
        )

        log.info("BOT RUNNING")

        while True:
            time.sleep(1)


# =============================
# START
# =============================
if __name__ == "__main__":
    Bot().run() 