from binance.client import Client
from binance.exceptions import BinanceAPIException
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

LEVERAGE = 2
FEE_RATE = 0.0004

TP_MULT = 1.5
SL_MULT = 1.0

EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

TICK_SLEEP = 10
KLINE_SLEEP = 300

MAX_RETRIES = 5

client = Client(API_KEY, API_SECRET, testnet=True)

# =============================
# SAFE API WRAPPER
# =============================
def safe_api_call(func):
    for i in range(MAX_RETRIES):
        try:
            return func()
        except BinanceAPIException as e:
            if "502" in str(e):
                print(f"⚠️ 502 Error, retry {i+1}")
                time.sleep(2 ** i)
            else:
                print(f"❌ API Error: {e}")
                return None
        except Exception as e:
            print(f"❌ Network Error: {e}")
            time.sleep(2 ** i)

    return None

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
class FuturesVWAPBot:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.position = None
        self.entry_price = None
        self.sl = None
        self.tp = None
        self.position_size = None

        self.logfile = f"trade_log_{SYMBOL}.csv"

        with open(self.logfile, "w", newline="") as f:
            csv.writer(f).writerow(
                ["Time","Symbol","Side","Entry","Exit","GrossPnL","Fee","NetPnL","Balance"]
            )

        print("✅ Futures VWAP Bot Started")

    # -------------------------
    def fetch_klines(self):

        data = safe_api_call(lambda: client.get_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=1000
        ))

        if not data:
            return None

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df[["open","high","low","close","volume"]] = df[
            ["open","high","low","close","volume"]
        ].astype(float)

        return df

    # -------------------------
    def fetch_price(self):

        data = safe_api_call(lambda: client.get_symbol_ticker(symbol=SYMBOL))

        if not data:
            return None

        return float(data["price"])

    # -------------------------
    def check_entry(self):

        if self.position:
            return

        df = self.fetch_klines()

        if df is None or df.empty:
            print("⚠️ No data, skipping entry")
            return

        df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
        df["RSI"] = rsi(df["close"], RSI_PERIOD)
        df["ATR"] = atr(df, ATR_PERIOD)
        df["VWAP"] = vwap(df)

        df = df.dropna()

        if df.empty:
            print("⚠️ Not enough data after indicators")
            return

        last = df.iloc[-1]
        price = last["close"]

        print(f"Price={price:.2f}, EMA={last['EMA']:.2f}, VWAP={last['VWAP']:.2f}, RSI={last['RSI']:.2f}")

        # LONG
        if price > last["EMA"] and price < last["VWAP"] * 1.002 and last["RSI"] < 45:
            self.open_trade("LONG", price, last["ATR"])

        # SHORT
        elif price < last["EMA"] and price > last["VWAP"] * 0.998 and last["RSI"] > 55:
            self.open_trade("SHORT", price, last["ATR"])

    # -------------------------
    def open_trade(self, side, price, atr_value):

        if atr_value is None or atr_value == 0:
            print("⚠️ Invalid ATR")
            return

        self.position = side
        self.entry_price = price

        if side == "LONG":
            self.sl = price - atr_value * SL_MULT
            self.tp = price + atr_value * TP_MULT
        else:
            self.sl = price + atr_value * SL_MULT
            self.tp = price - atr_value * TP_MULT

        position_value = self.balance * LEVERAGE
        self.position_size = position_value / price

        print(f"📈 {side} ENTRY @ {price:.2f}")
        print(f"🎯 TP={self.tp:.2f} | 🛑 SL={self.sl:.2f}")

    # -------------------------
    def manage_trade(self):

        if not self.position:
            return

        price = self.fetch_price()

        if price is None:
            print("⚠️ Price fetch failed")
            return

        # STOP LOSS & TAKE PROFIT CHECK
        if self.position == "LONG":
            if price <= self.sl:
                print("🛑 STOP LOSS HIT")
                pnl = (price - self.entry_price) * self.position_size
                self.close_trade(price, pnl)

            elif price >= self.tp:
                print("🎯 TAKE PROFIT HIT")
                pnl = (price - self.entry_price) * self.position_size
                self.close_trade(price, pnl)

        elif self.position == "SHORT":
            if price >= self.sl:
                print("🛑 STOP LOSS HIT")
                pnl = (self.entry_price - price) * self.position_size
                self.close_trade(price, pnl)

            elif price <= self.tp:
                print("🎯 TAKE PROFIT HIT")
                pnl = (self.entry_price - price) * self.position_size
                self.close_trade(price, pnl)

    # -------------------------
    def close_trade(self, price, pnl):

        entry_value = self.position_size * self.entry_price
        exit_value = self.position_size * price

        fee = (entry_value + exit_value) * FEE_RATE

        net_pnl = pnl - fee
        prev_balance = self.balance
        self.balance += net_pnl

        with open(self.logfile, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.utcnow(),
                SYMBOL,
                self.position,
                self.entry_price,
                price,
                pnl,
                fee,
                net_pnl,
                self.balance
            ])

        print("\n💰 TRADE CLOSED")
        print(f"Balance: {prev_balance:.2f} → {self.balance:.2f}\n")

        self.position = None

    # -------------------------
    def run(self):

        print("🚀 Bot running...")
        last_kline = 0

        while True:
            try:
                now = time.time()

                if now - last_kline >= KLINE_SLEEP:
                    self.check_entry()
                    last_kline = now

                self.manage_trade()

            except Exception as e:
                print(f"🚨 ERROR: {e}")

            time.sleep(TICK_SLEEP)

# =============================
FuturesVWAPBot().run()