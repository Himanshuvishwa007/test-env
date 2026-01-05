from binance.client import Client
import pandas as pd
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
SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

INITIAL_BALANCE = 10000.0
LEVERAGE = 2

TAKER_FEE = 0.0007     # realistic futures taker fee
TP_PCT = 0.008         # 0.8%
SL_PCT = 0.004         # 0.4%
HARD_SL_PCT = 0.005    # 0.5% absolute cap

MAX_HOLD_SECONDS = 45 * 60

EMA_TREND = 50
RSI_PERIOD = 14
VOL_WINDOW = 20
VOL_MULT = 1.3

RSI_LONG_MIN = 54
RSI_SHORT_MAX = 46

KLINE_SLEEP = 300      # 5 min
TICK_SLEEP = 5         # tick risk loop

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
# BOT
# =============================
class MomentumFuturesPaperBot:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.position = None
        self.entry_price = None
        self.entry_time = None

        self.realized_pnl = 0.0
        self.trades = 0
        self.last_kline_time = None

        with open("trade_log_momentum_futures.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Time", "Side", "EntryPrice", "ExitPrice",
                "TradePnL", "NetPnL", "Equity"
            ])

        print("✅ Momentum Futures Paper Bot STARTED")
        print("📌 ETHUSDT | 2× leverage | Tick-synced risk\n")

    # -------------------------
    # DATA
    # -------------------------
    def fetch_klines(self):
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

    def fetch_tick_price(self):
        return float(client.get_symbol_ticker(symbol=SYMBOL)["price"])

    # -------------------------
    # LOGGING
    # -------------------------
    def log_trade(self, side, exit_price, pnl):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        with open("trade_log_momentum_futures.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                now, side, self.entry_price, exit_price,
                pnl, self.realized_pnl, self.balance
            ])

    # -------------------------
    # ENTRY (5-MIN SYNCED)
    # -------------------------
    def check_entry(self):
        df = self.fetch_klines()
        last = df.iloc[-2]  # last CLOSED candle

        if self.last_kline_time == last["time"]:
            return

        self.last_kline_time = last["time"]

        df["EMA"] = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
        df["RSI"] = rsi(df["close"], RSI_PERIOD)
        df["VOL_SMA"] = df["volume"].rolling(VOL_WINDOW).mean()

        if self.position is not None:
            return

        price = last["close"]

        if (
            price > df.iloc[-2]["EMA"] and
            last["RSI"] > RSI_LONG_MIN and
            last["volume"] > df.iloc[-2]["VOL_SMA"] * VOL_MULT
        ):
            self.position = "LONG"
            self.entry_price = price
            self.entry_time = time.time()
            print(f"📈 LONG @ {price:.2f}")

        elif (
            price < df.iloc[-2]["EMA"] and
            last["RSI"] < RSI_SHORT_MAX and
            last["volume"] > df.iloc[-2]["VOL_SMA"] * VOL_MULT
        ):
            self.position = "SHORT"
            self.entry_price = price
            self.entry_time = time.time()
            print(f"📉 SHORT @ {price:.2f}")

    # -------------------------
    # TICK-BY-TICK RISK
    # -------------------------
    def manage_risk(self):
        if self.position is None:
            return

        price = self.fetch_tick_price()
        hold_time = time.time() - self.entry_time

        position_size = (self.balance * LEVERAGE) / self.entry_price

        if self.position == "LONG":
            tp = self.entry_price * (1 + TP_PCT)
            sl = self.entry_price * (1 - SL_PCT)
            hard_sl = self.entry_price * (1 - HARD_SL_PCT)

            exit_reason = None
            if price >= tp:
                exit_reason = "TP"
            elif price <= sl:
                exit_reason = "SL"
            elif price <= hard_sl:
                exit_reason = "HARD_SL"
            elif hold_time >= MAX_HOLD_SECONDS:
                exit_reason = "TIME"

            if exit_reason:
                gross = (price - self.entry_price) * position_size
                fee = (self.entry_price + price) * position_size * TAKER_FEE
                pnl = gross - fee

                self.balance += pnl
                self.realized_pnl += pnl
                self.trades += 1

                print(f"💰 EXIT LONG ({exit_reason}) @ {price:.2f} | PnL={pnl:.2f}")
                self.log_trade("LONG", price, pnl)
                self.position = None

        elif self.position == "SHORT":
            tp = self.entry_price * (1 - TP_PCT)
            sl = self.entry_price * (1 + SL_PCT)
            hard_sl = self.entry_price * (1 + HARD_SL_PCT)

            exit_reason = None
            if price <= tp:
                exit_reason = "TP"
            elif price >= sl:
                exit_reason = "SL"
            elif price >= hard_sl:
                exit_reason = "HARD_SL"
            elif hold_time >= MAX_HOLD_SECONDS:
                exit_reason = "TIME"

            if exit_reason:
                gross = (self.entry_price - price) * position_size
                fee = (self.entry_price + price) * position_size * TAKER_FEE
                pnl = gross - fee

                self.balance += pnl
                self.realized_pnl += pnl
                self.trades += 1

                print(f"💰 EXIT SHORT ({exit_reason}) @ {price:.2f} | PnL={pnl:.2f}")
                self.log_trade("SHORT", price, pnl)
                self.position = None

    # -------------------------
    # RUN LOOP
    # -------------------------
    def run(self):
        print("🚀 Bot running (5m entry + tick risk)\n")

        while True:
            try:
                self.check_entry()
                self.manage_risk()

                print(
                    f"[Tick] Pos={self.position} | Balance={self.balance:.2f} "
                    f"| NetPnL={self.realized_pnl:.2f} | Trades={self.trades}"
                )

                time.sleep(TICK_SLEEP)

            except Exception as e:
                print(f"⚠️ Error: {e}")
                time.sleep(5)

# =============================
# START BOT
# =============================
bot = MomentumFuturesPaperBot()
bot.run()
