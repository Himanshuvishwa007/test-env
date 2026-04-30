import os
import time
import math
import logging
import pandas as pd
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException, BinanceRequestException

# =============================
# CONFIG
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
LEVERAGE = 2

EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

TP_MULT = 1.5
SL_MULT = 1.0

SLEEP = 60
MAX_RETRIES = 5
SLIPPAGE_THRESHOLD = 0.002

# =============================
# LOGGING
# =============================
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

def log(msg):
    print(msg)
    logging.info(msg)

client = Client(API_KEY, API_SECRET)

# =============================
# SAFE CALL
# =============================
def safe_call(func):
    for i in range(MAX_RETRIES):
        try:
            return func()
        except (BinanceAPIException, BinanceRequestException) as e:
            log(f"Retry {i+1}: {e}")
            time.sleep(2 ** i)
        except Exception as e:
            log(f"Unknown error {i+1}: {e}")
            time.sleep(2 ** i)
    return None

# =============================
# GET SYMBOL FILTERS
# =============================
def get_filters():
    info = safe_call(lambda: client.futures_exchange_info())

    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            filters = {f["filterType"]: f for f in s["filters"]}

            step = float(filters["LOT_SIZE"]["stepSize"])
            min_qty = float(filters["LOT_SIZE"]["minQty"])
            tick = float(filters["PRICE_FILTER"]["tickSize"])
            min_notional = float(filters["MIN_NOTIONAL"]["notional"])

            return step, min_qty, tick, min_notional

    return 0.001, 0.001, 0.1, 20

STEP, MIN_QTY, TICK, MIN_NOTIONAL = get_filters()

# =============================
# PRECISION SAFE ROUNDING
# =============================
def round_qty(qty):
    precision = int(round(-math.log10(STEP)))
    qty = math.floor(qty / STEP) * STEP

    return float(
        Decimal(str(qty)).quantize(
            Decimal(str(STEP)),
            rounding=ROUND_DOWN
        )
    )

def round_price(price):
    return round(price / TICK) * TICK

# =============================
# QTY ADJUSTMENT (FIXED)
# =============================
def adjust_qty(qty, price):
    qty = max(qty, MIN_QTY)

    # enforce notional
    if qty * price < MIN_NOTIONAL:
        qty = math.ceil((MIN_NOTIONAL / price) / STEP) * STEP

    qty = round_qty(qty)

    # final safety check
    if qty * price < MIN_NOTIONAL:
        return None

    return qty

# =============================
# INDICATORS
# =============================
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(period).mean() / loss.rolling(period).mean()
    return 100 - (100/(1+rs))

def atr(df, period):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def vwap(df):
    pv = (df["close"] * df["volume"]).rolling(VWAP_WINDOW).sum()
    vol = df["volume"].rolling(VWAP_WINDOW).sum()
    return pv / vol

# =============================
# BOT CLASS
# =============================
class Bot:

    def __init__(self):
        self.position = None
        self.qty = 0
        self.entry_price = 0
        self.last_price = None

        self.set_leverage()
        self.sync_position()

    def set_leverage(self):
        safe_call(lambda: client.futures_change_leverage(
            symbol=SYMBOL,
            leverage=LEVERAGE
        ))
        log(f"Leverage set {LEVERAGE}x")

    def get_balance(self):
        data = safe_call(lambda: client.futures_account_balance())
        for b in data:
            if b["asset"] in ["USDT", "FDUSD"]:
                return float(b["balance"])
        return 0

    def sync_position(self):
        pos = safe_call(lambda: client.futures_position_information(symbol=SYMBOL))
        for p in pos:
            amt = float(p["positionAmt"])
            if amt != 0:
                self.qty = abs(amt)
                self.entry_price = float(p["entryPrice"])
                self.position = "LONG" if amt > 0 else "SHORT"
                log(f"Synced: {self.position} {self.qty}")

    def fetch_klines(self):
        data = safe_call(lambda: client.futures_klines(
            symbol=SYMBOL,
            interval="5m",
            limit=300
        ))

        if not data:
            return None

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        df[["open","high","low","close","volume"]] = df[
            ["open","high","low","close","volume"]
        ].astype(float)

        return df

    # =============================
    # TRADE EXECUTION (FIXED)
    # =============================
    def open_trade(self, side, price, atr_val):

        if self.position:
            return

        balance = self.get_balance()

        raw_qty = (balance * LEVERAGE) / price
        qty = adjust_qty(raw_qty, price)

        if qty is None:
            log("Qty too small after adjustment (MIN_NOTIONAL fail)")
            return

        log(f"Final Qty: {qty}, Notional: {qty * price}")

        self.last_price = price

        try:
            order = safe_call(lambda: client.futures_create_order(
                symbol=SYMBOL,
                side="BUY" if side == "LONG" else "SELL",
                type="MARKET",
                quantity=qty
            ))

            if not order:
                return

            avg_price = float(order.get("avgPrice", price))

            self.position = side
            self.qty = qty
            self.entry_price = avg_price

            if side == "LONG":
                sl = avg_price - atr_val * SL_MULT
                tp = avg_price + atr_val * TP_MULT
            else:
                sl = avg_price + atr_val * SL_MULT
                tp = avg_price - atr_val * TP_MULT

            self.place_sl_tp(side, sl, tp)

            log(f"{side} @ {avg_price} qty={qty}")

        except Exception as e:
            log(e)

    def place_sl_tp(self, side, sl, tp):

        safe_call(lambda: client.futures_create_order(
            symbol=SYMBOL,
            side="SELL" if side == "LONG" else "BUY",
            type="STOP_MARKET",
            stopPrice=round_price(sl),
            closePosition=True
        ))

        safe_call(lambda: client.futures_create_order(
            symbol=SYMBOL,
            side="SELL" if side == "LONG" else "BUY",
            type="TAKE_PROFIT_MARKET",
            stopPrice=round_price(tp),
            closePosition=True
        ))

    def run(self):
        log("Bot started")

        while True:
            try:
                df = self.fetch_klines()
                if df is None:
                    continue

                df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
                df["RSI"] = rsi(df["close"], RSI_PERIOD)
                df["ATR"] = atr(df, ATR_PERIOD)
                df["VWAP"] = vwap(df)

                df = df.dropna()
                last = df.iloc[-1]

                price = last["close"]

                if price > last["EMA"] and last["RSI"] < 45:
                    self.open_trade("LONG", price, last["ATR"])

                elif price < last["EMA"] and last["RSI"] > 55:
                    self.open_trade("SHORT", price, last["ATR"])

            except Exception as e:
                log(f"Loop error: {e}")

            time.sleep(SLEEP)

# =============================
Bot().run()