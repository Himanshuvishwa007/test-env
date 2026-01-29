from binance.client import Client
import pandas as pd
import numpy as np
import time
import csv
from datetime import datetime

# =============================
# BINANCE FUTURES TESTNET
# =============================
API_KEY = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
API_SECRET = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"

client = Client(API_KEY, API_SECRET, testnet=True)

# =============================
# CONFIG (FROZEN)
# =============================
SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

INITIAL_BALANCE = 10000
LEVERAGE = 2
MAKER_FEE = 0.0005

EMA_FAST = 50
EMA_SLOW = 200
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 20

RR_RATIO = 2.0     # Reward : Risk

KLINE_SLEEP = 300
TICK_SLEEP = 10

# =============================
# INDICATORS
# =============================
def atr(df, period):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# =============================
# BOT
# =============================
class ATRBreakoutPaperBot:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.position = None
        self.entry_price = None
        self.entry_time = None
        self.sl = None
        self.tp = None

        self.realized_pnl = 0.0
        self.trades = 0

        with open("trade_log_atr_breakout.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Time","Side","Entry","Exit",
                "PnL","NetPnL","Equity"
            ])

        print("✅ ATR BREAKOUT PAPER BOT STARTED")
        print("📌 Strategy frozen | Trend + ATR breakout\n")

    # -------------------------
    # DATA
    # -------------------------
    def fetch_klines(self):
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=EMA_SLOW + ATR_PERIOD + BREAKOUT_LOOKBACK + 5
        )

        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df[["open","high","low","close"]] = df[
            ["open","high","low","close"]
        ].astype(float)

        return df[["time","open","high","low","close"]]

    def fetch_tick_price(self):
        return float(client.get_symbol_ticker(symbol=SYMBOL)["price"])

    # -------------------------
    # LOGGING
    # -------------------------
    def log_trade(self, side, exit_price, pnl):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        equity = INITIAL_BALANCE + self.realized_pnl

        with open("trade_log_atr_breakout.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                now, side,
                self.entry_price, exit_price,
                pnl, self.realized_pnl, equity
            ])

    # -------------------------
    # ENTRY (5m candle close)
    # -------------------------
    def check_entry(self):
        if self.position is not None:
            return

        df = self.fetch_klines()

        df["EMA50"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["EMA200"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        df["ATR"] = atr(df, ATR_PERIOD)

        last = df.iloc[-2]   # CLOSED candle
        prev = df.iloc[-3]

        high_range = df["high"].iloc[-BREAKOUT_LOOKBACK-2:-2].max()
        low_range = df["low"].iloc[-BREAKOUT_LOOKBACK-2:-2].min()

        price = last["close"]

        # ===== LONG =====
        if (
            last["EMA50"] > last["EMA200"] and
            price > high_range and
            last["ATR"] > prev["ATR"]
        ):
            self.position = "LONG"
            self.entry_price = price
            self.entry_time = last["time"]
            self.sl = price - last["ATR"]
            self.tp = price + RR_RATIO * last["ATR"]

            print(f"📈 LONG @ {price:.2f}")

        # ===== SHORT =====
        elif (
            last["EMA50"] < last["EMA200"] and
            price < low_range and
            last["ATR"] > prev["ATR"]
        ):
            self.position = "SHORT"
            self.entry_price = price
            self.entry_time = last["time"]
            self.sl = price + last["ATR"]
            self.tp = price - RR_RATIO * last["ATR"]

            print(f"📉 SHORT @ {price:.2f}")

    # -------------------------
    # RISK MGMT (tick)
    # -------------------------
    def manage_risk(self):
        if self.position is None:
            return

        price = self.fetch_tick_price()

        if self.position == "LONG" and (price <= self.sl or price >= self.tp):
            gross = (price - self.entry_price) * LEVERAGE
            fee = (price + self.entry_price) * LEVERAGE * MAKER_FEE
            pnl = gross - fee

            self.realized_pnl += pnl
            self.trades += 1

            print(f"💰 EXIT LONG @ {price:.2f} | PnL={pnl:.2f}")
            self.log_trade("LONG", price, pnl)
            self.position = None

        elif self.position == "SHORT" and (price >= self.sl or price <= self.tp):
            gross = (self.entry_price - price) * LEVERAGE
            fee = (price + self.entry_price) * LEVERAGE * MAKER_FEE
            pnl = gross - fee

            self.realized_pnl += pnl
            self.trades += 1

            print(f"💰 EXIT SHORT @ {price:.2f} | PnL={pnl:.2f}")
            self.log_trade("SHORT", price, pnl)
            self.position = None

    # -------------------------
    # RUN LOOP
    # -------------------------
    def run(self):
        print("🚀 Bot running (Always ON)\n")
        last_kline_check = 0

        while True:
            try:
                now = time.time()

                if now - last_kline_check >= KLINE_SLEEP:
                    self.check_entry()
                    last_kline_check = now

                self.manage_risk()

                equity = INITIAL_BALANCE + self.realized_pnl
                print(
                    f"[Tick] Pos={self.position} | "
                    f"PnL={self.realized_pnl:.2f} | "
                    f"Equity={equity:.2f} | "
                    f"Trades={self.trades}"
                )

                time.sleep(TICK_SLEEP)

            except Exception as e:
                print(f"⚠️ Error: {e}")
                time.sleep(5)

# =============================
# START BOT
# =============================
bot = ATRBreakoutPaperBot()
bot.run()
