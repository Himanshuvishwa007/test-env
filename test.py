from binance.client import Client
import pandas as pd
import numpy as np
import time
import csv
from datetime import datetime

# =============================
# BINANCE TESTNET (FUTURES PAPER)
# =============================
API_KEY = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
API_SECRET = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"

client = Client(API_KEY, API_SECRET, testnet=True)

# =============================
# CONFIG
# =============================
SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

INITIAL_BALANCE = 10000
LEVERAGE = 2

MAKER_FEE = 0.0005   # 0.05%

TP_PCT = 0.008       # 0.8%
SL_PCT = 0.004       # 0.4%

EMA_TREND = 50
RSI_PERIOD = 14
VOL_WINDOW = 20
VOL_MULT = 1.3

RSI_LONG_MIN = 54
RSI_SHORT_MAX = 46

# =============================
# INDICATORS
# =============================
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =============================
# MOMENTUM FUTURES BOT
# =============================
class MomentumFuturesPaperBot:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.position = None  # LONG / SHORT
        self.entry_price = None
        self.entry_time = None

        self.realized_pnl = 0.0
        self.trades = 0
        self.equity_curve = [INITIAL_BALANCE]

        self.df = pd.DataFrame()

        with open("trade_log_momentum_futures.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Time", "Side", "EntryPrice", "ExitPrice",
                "TradePnL", "NetPnL", "Equity"
            ])

        print("✅ Momentum Futures Paper Bot Initialized")
        print(f"⚙️ EMA={EMA_TREND}, RSI={RSI_PERIOD}, TP={TP_PCT*100:.2f}%, SL={SL_PCT*100:.2f}%")
        print(f"⚙️ Leverage={LEVERAGE}x | Maker Fee={MAKER_FEE*100:.2f}%\n")

    def fetch_latest_candle(self):
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=EMA_TREND + VOL_WINDOW + RSI_PERIOD + 5
        )

        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df[["open","high","low","close","volume"]] = df[
            ["open","high","low","close","volume"]
        ].astype(float)

        return df[["time","open","high","low","close","volume"]]

    def log_trade(self, side, exit_price, trade_pnl):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        equity = INITIAL_BALANCE + self.realized_pnl

        with open("trade_log_momentum_futures.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                now, side, self.entry_price, exit_price,
                trade_pnl, self.realized_pnl, equity
            ])

    def run(self):
        print("🚀 Starting Momentum Futures Paper Trading (Always ON)\n")

        while True:
            try:
                df = self.fetch_latest_candle()
                self.df = df.copy()

                df["EMA"] = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
                df["RSI"] = rsi(df["close"], RSI_PERIOD)
                df["VOL_SMA"] = df["volume"].rolling(VOL_WINDOW).mean()

                last = df.iloc[-1]
                price = last["close"]

                # ================= ENTRY =================
                if self.position is None:
                    if (
                        price > last["EMA"] and
                        last["RSI"] > RSI_LONG_MIN and
                        last["volume"] > last["VOL_SMA"] * VOL_MULT
                    ):
                        self.position = "LONG"
                        self.entry_price = price
                        self.entry_time = last["time"]
                        print(f"📈 LONG @ {price:.2f}")

                    elif (
                        price < last["EMA"] and
                        last["RSI"] < RSI_SHORT_MAX and
                        last["volume"] > last["VOL_SMA"] * VOL_MULT
                    ):
                        self.position = "SHORT"
                        self.entry_price = price
                        self.entry_time = last["time"]
                        print(f"📉 SHORT @ {price:.2f}")

                # ================= EXIT =================
                else:
                    if self.position == "LONG":
                        tp = self.entry_price * (1 + TP_PCT)
                        sl = self.entry_price * (1 - SL_PCT)

                        if price >= tp or price <= sl:
                            gross = (price - self.entry_price) * LEVERAGE
                            fee = (self.entry_price + price) * LEVERAGE * MAKER_FEE
                            trade_pnl = gross - fee

                            self.realized_pnl += trade_pnl
                            self.trades += 1

                            print(f"💰 EXIT LONG @ {price:.2f} | PnL={trade_pnl:.2f}")
                            self.log_trade("LONG", price, trade_pnl)
                            self.position = None

                    elif self.position == "SHORT":
                        tp = self.entry_price * (1 - TP_PCT)
                        sl = self.entry_price * (1 + SL_PCT)

                        if price <= tp or price >= sl:
                            gross = (self.entry_price - price) * LEVERAGE
                            fee = (self.entry_price + price) * LEVERAGE * MAKER_FEE
                            trade_pnl = gross - fee

                            self.realized_pnl += trade_pnl
                            self.trades += 1

                            print(f"💰 EXIT SHORT @ {price:.2f} | PnL={trade_pnl:.2f}")
                            self.log_trade("SHORT", price, trade_pnl)
                            self.position = None

                equity = INITIAL_BALANCE + self.realized_pnl
                self.equity_curve.append(equity)

                print(
                    f"[Price={price:.2f}] Pos={self.position} | "
                    f"NetPnL={self.realized_pnl:.2f} | Equity={equity:.2f} | Trades={self.trades}"
                )

                time.sleep(300)  # 5 minutes

            except Exception as e:
                print(f"⚠️ Error: {e}")
                time.sleep(10)

# =============================
# RUN
# =============================
bot = MomentumFuturesPaperBot()
bot.run()
