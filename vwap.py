from binance.client import Client
import pandas as pd
import time
import csv
from datetime import datetime

# =============================
# CONFIG
# =============================
API_KEY = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
API_SECRET = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"

SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

INITIAL_BALANCE = 10000
RISK_PER_TRADE = 0.01

TP_MULT = 1.5
SL_MULT = 1.0

EMA_TREND = 100   # 🔥 faster than 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

TICK_SLEEP = 10
KLINE_SLEEP = 300

client = Client(API_KEY, API_SECRET, testnet=True)

# =============================
# INDICATORS
# =============================
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(period).mean() / loss.rolling(period).mean()
    return 100 - (100 / (1 + rs))

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
# BOT
# =============================
class VWAPMeanReversionBot:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.position = None
        self.entry_price = None
        self.sl = None
        self.tp = None
        self.position_size = None  # ✅ FIXED

        self.logfile = f"trade_log_{SYMBOL}.csv"

        with open(self.logfile, "w", newline="") as f:
            csv.writer(f).writerow(
                ["Time","Symbol","Side","Entry","Exit","PnL","Equity"]
            )

        print("✅ VWAP Mean Reversion Paper Bot Started")

    # -------------------------
    def fetch_klines(self):
        df = pd.DataFrame(
            client.get_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=1000
            ),
            columns=[
                "time","open","high","low","close","volume",
                "_","_","_","_","_","_"
            ]
        )

        df["time"] = pd.to_datetime(df["time"], unit="ms")

        df[["open","high","low","close","volume"]] = df[
            ["open","high","low","close","volume"]
        ].astype(float)

        return df

    def fetch_price(self):
        return float(client.get_symbol_ticker(symbol=SYMBOL)["price"])

    # -------------------------
    def check_entry(self):

        df = self.fetch_klines()

        df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
        df["RSI"] = rsi(df["close"], RSI_PERIOD)
        df["ATR"] = atr(df, ATR_PERIOD)
        df["VWAP"] = vwap(df)

        df = df.dropna()
        last = df.iloc[-1]

        price = last["close"]

        # 🔍 DEBUG (very important)
        print(f"Price={price:.2f}, EMA={last['EMA']:.2f}, VWAP={last['VWAP']:.2f}, RSI={last['RSI']:.2f}")

        if self.position:
            return

        # =====================
        # IMPROVED CONDITIONS
        # =====================

        # LONG
        if (
            price > last["EMA"]
            and price < last["VWAP"] * 1.002   # 🔥 tolerance
            and last["RSI"] < 45
        ):
            self.open_trade("LONG", price, last["ATR"])

        # SHORT
        elif (
            price < last["EMA"]
            and price > last["VWAP"] * 0.998
            and last["RSI"] > 55
        ):
            self.open_trade("SHORT", price, last["ATR"])

    # -------------------------
    def open_trade(self, side, price, atr_value):

        self.position = side
        self.entry_price = price

        if side == "LONG":
            self.sl = price - atr_value * SL_MULT
            self.tp = price + atr_value * TP_MULT
        else:
            self.sl = price + atr_value * SL_MULT
            self.tp = price - atr_value * TP_MULT

        # ✅ FIX: position size locked here
        risk_amount = self.balance * RISK_PER_TRADE
        self.position_size = risk_amount / abs(price - self.sl)

        print(f"📈 {side} ENTRY @ {price:.2f}")

    # -------------------------
    def manage_trade(self):

        if not self.position:
            return

        price = self.fetch_price()

        if self.position == "LONG":
            if price >= self.tp or price <= self.sl:
                pnl = (price - self.entry_price) * self.position_size
                self.close_trade(price, pnl)

        else:
            if price <= self.tp or price >= self.sl:
                pnl = (self.entry_price - price) * self.position_size
                self.close_trade(price, pnl)

    # -------------------------
    def close_trade(self, price, pnl):

        self.balance += pnl

        with open(self.logfile, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.utcnow(),
                SYMBOL,
                self.position,
                self.entry_price,
                price,
                pnl,
                self.balance
            ])

        print(f"💰 EXIT {self.position} | PnL = {pnl:.2f} | Balance = {self.balance:.2f}")

        self.position = None

    # -------------------------
    def run(self):

        print("🚀 Bot running...")
        last_kline = 0

        while True:

            now = time.time()

            if now - last_kline >= KLINE_SLEEP:
                self.check_entry()
                last_kline = now

            self.manage_trade()
            time.sleep(TICK_SLEEP)


# =============================
VWAPMeanReversionBot().run()