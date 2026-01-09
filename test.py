from binance.client import Client
import pandas as pd
import time
import csv
from datetime import datetime, date

# =============================
# CONFIG
# =============================
API_KEY = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
API_SECRET = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"

SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

INITIAL_BALANCE = 10000.0
LEVERAGE = 2

# ---- PRODUCTION RISK ----
RISK_PER_TRADE = 0.01        # 1% risk
MAX_DAILY_LOSS = 0.03        # 3% daily loss
MAX_DRAWDOWN = 0.20          # 20% drawdown kill switch

TP_PCT = 0.008
SL_PCT = 0.004
MAKER_FEE = 0.0005

EMA_TREND = 50
EMA_SLOPE_LOOKBACK = 3

RSI_PERIOD = 14
VOL_WINDOW = 20
VOL_MULT = 1.3

RSI_LONG_MIN = 54
RSI_SHORT_MAX = 46

# ---- MODE SWITCH ----
ENABLE_LONGS = False     # 🔴 disabled for testing
ENABLE_SHORTS = True

KLINE_SLEEP = 300        # 5 min
TICK_SLEEP = 10          # tick risk checks

# =============================
# CLIENT (TESTNET / PAPER)
# =============================
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

# =============================
# BOT
# =============================
class MomentumFuturesPaperBot:

    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.peak_equity = INITIAL_BALANCE
        self.daily_start_balance = INITIAL_BALANCE
        self.today = date.today()

        self.position = None
        self.entry_price = None

        self.realized_pnl = 0.0
        self.trades = 0

        self.logfile = f"trade_log_{SYMBOL}_{int(time.time())}.csv"

        with open(self.logfile, "w", newline="") as f:
            csv.writer(f).writerow([
                "Time","Symbol","Side","Entry","Exit","PnL","Equity"
            ])

        print("✅ Momentum Futures Paper Bot STARTED")
        print("📌 ETHUSDT | SHORT-only | EMA slope + risk guardrails\n")

    # -------------------------
    # DATA
    # -------------------------
    def fetch_klines(self):
        df = pd.DataFrame(
            client.get_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=120
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
    # ENTRY (5m candles)
    # -------------------------
    def check_entry(self):
        df = self.fetch_klines().dropna()

        # hard guardrail
        if len(df) < 120 or self.position:
            return

        df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
        df["EMA_SLOPE"] = df["EMA"] - df["EMA"].shift(EMA_SLOPE_LOOKBACK)
        df["RSI"] = rsi(df["close"], RSI_PERIOD)
        df["VOL_SMA"] = df["volume"].rolling(VOL_WINDOW).mean()

        df = df.dropna()
        if df.empty:
            return

        last = df.iloc[-1]
        price = last["close"]

        # -------- SHORT ONLY (FILTERED) --------
        if (
            ENABLE_SHORTS
            and price < last["EMA"]
            and last["EMA_SLOPE"] < 0
            and last["RSI"] < RSI_SHORT_MAX
            and last["volume"] > last["VOL_SMA"] * VOL_MULT
        ):
            self.open_position("SHORT", price)

        # -------- LONG (DISABLED) --------
        elif (
            ENABLE_LONGS
            and price > last["EMA"]
            and last["EMA_SLOPE"] > 0
            and last["RSI"] > RSI_LONG_MIN
            and last["volume"] > last["VOL_SMA"] * VOL_MULT
        ):
            self.open_position("LONG", price)

    def open_position(self, side, price):
        self.position = side
        self.entry_price = price
        print(f"📉 {side} @ {price:.2f}")

    # -------------------------
    # RISK MANAGEMENT (tick)
    # -------------------------
    def manage_risk(self):
        if not self.position:
            return

        price = self.fetch_price()

        risk_amount = self.balance * RISK_PER_TRADE
        position_size = risk_amount / (self.entry_price * SL_PCT)

        if self.position == "LONG":
            hit = price >= self.entry_price * (1 + TP_PCT) or price <= self.entry_price * (1 - SL_PCT)
            pnl = (price - self.entry_price) * position_size
        else:
            hit = price <= self.entry_price * (1 - TP_PCT) or price >= self.entry_price * (1 + SL_PCT)
            pnl = (self.entry_price - price) * position_size

        if hit:
            self.close_trade(price, pnl)

    def close_trade(self, price, pnl):
        self.balance += pnl
        self.realized_pnl += pnl
        self.trades += 1
        self.peak_equity = max(self.peak_equity, self.balance)

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

        print(f"💰 EXIT {self.position} | PnL={pnl:.2f}")
        self.position = None
        self.check_kill_switch()

    # -------------------------
    # KILL SWITCHES
    # -------------------------
    def check_kill_switch(self):
        drawdown = (self.peak_equity - self.balance) / self.peak_equity
        daily_loss = (self.daily_start_balance - self.balance) / self.daily_start_balance

        if drawdown >= MAX_DRAWDOWN:
            raise SystemExit("🛑 MAX DRAWDOWN HIT")

        if daily_loss >= MAX_DAILY_LOSS:
            raise SystemExit("🛑 DAILY LOSS LIMIT HIT")

    # -------------------------
    # RUN LOOP
    # -------------------------
    def run(self):
        print("🚀 Bot running (5m entry + tick risk)\n")
        last_kline = 0

        while True:
            now = time.time()

            if now - last_kline >= KLINE_SLEEP:
                self.check_entry()
                last_kline = now

            self.manage_risk()
            time.sleep(TICK_SLEEP)

# =============================
# START
# =============================
MomentumFuturesPaperBot().run()
