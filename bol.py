from binance.client import Client
import pandas as pd
import numpy as np
import time
import csv
from datetime import datetime

# -----------------------------
# Binance Testnet (Paper)
# -----------------------------
api_key = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
api_secret = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"
client = Client(api_key, api_secret, testnet=True)
class BB_RSI_Volume_PaperBot:

    def __init__(
        self,
        api_key,
        api_secret,
        symbol="BTCUSDT",
        interval=Client.KLINE_INTERVAL_1MINUTE,

        # ---- Strategy Controls ----
        bb_window=20,
        bb_std=2,
        rsi_period=14,
        vol_window=20,
        vol_multiplier=1.5,
        trend_sma=50,

        # ---- Risk Controls ----
        tp_pct=0.07,
        sl_pct=0.03,
        fee=0.001,

        initial_cash=10000
    ):

        self.client = Client(api_key, api_secret, testnet=True)
        self.symbol = symbol
        self.interval = interval

        self.bb_window = bb_window
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.vol_window = vol_window
        self.vol_multiplier = vol_multiplier
        self.trend_sma = trend_sma

        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.fee = fee

        self.initial_cash = initial_cash
        self.position = 0
        self.entry_price = None
        self.realized_pnl = 0
        self.trades = 0

        self.data = pd.DataFrame()

        with open("trade_log_bb_rsi_volume.csv", "w", newline="") as f:
            csv.writer(f).writerow(
                ["Time", "Action", "Price", "TradePnL", "NetPnL"]
            )

        print(
            f"✅ BB+RSI+Volume PAPER BOT Started\n"
            f"BB({bb_window},{bb_std}) | RSI({rsi_period}) | "
            f"Vol x{vol_multiplier} | Trend SMA({trend_sma}) | "
            f"TP {tp_pct*100:.1f}% | SL {sl_pct*100:.1f}%"
        )

    # -----------------------------
    # RSI
    # -----------------------------
    def compute_rsi(self, series):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(self.rsi_period).mean()
        avg_loss = loss.rolling(self.rsi_period).mean()

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    # -----------------------------
    # Fetch latest candle
    # -----------------------------
    def fetch_candle(self):
        k = self.client.get_klines(
            symbol=self.symbol,
            interval=self.interval,
            limit=1
        )[0]

        return {
            "time": pd.to_datetime(k[0], unit="ms"),
            "close": float(k[4]),
            "volume": float(k[5])
        }

    # -----------------------------
    # Core Strategy
    # -----------------------------
    def process(self):
        candle = self.fetch_candle()
        self.data = pd.concat(
            [self.data, pd.DataFrame([candle])],
            ignore_index=True
        )

        min_len = max(
            self.bb_window,
            self.rsi_period,
            self.vol_window,
            self.trend_sma
        )

        if len(self.data) < min_len:
            return

        df = self.data.copy()

        # ---- Indicators ----
        df["SMA"] = df["close"].rolling(self.bb_window).mean()
        df["STD"] = df["close"].rolling(self.bb_window).std()
        df["Upper"] = df["SMA"] + self.bb_std * df["STD"]
        df["Lower"] = df["SMA"] - self.bb_std * df["STD"]

        df["RSI"] = self.compute_rsi(df["close"])
        df["Vol_SMA"] = df["volume"].rolling(self.vol_window).mean()
        df["Trend_SMA"] = df["close"].rolling(self.trend_sma).mean()

        last = df.iloc[-1]
        price = last["close"]

        # ---- BUY CONDITIONS ----
        buy_signal = (
            price < last["Lower"] and
            last["RSI"] < 30 and
            last["volume"] > last["Vol_SMA"] * self.vol_multiplier and
            price > last["Trend_SMA"]
        )

        # ---- SELL CONDITIONS ----
        sell_signal = (
            price > last["SMA"] or
            last["RSI"] > 60
        )

        # ---- EXECUTION ----
        if buy_signal and self.position == 0:
            self.position = 1
            self.entry_price = price
            self.trades += 1
            self.log("BUY", price, 0)
            print(f"📈 BUY @ {price:.2f}")

        elif self.position == 1:
            tp = self.entry_price * (1 + self.tp_pct)
            sl = self.entry_price * (1 - self.sl_pct)

            exit_price = None
            reason = None

            if price >= tp:
                exit_price = tp
                reason = "TAKE_PROFIT"
            elif price <= sl:
                exit_price = sl
                reason = "STOP_LOSS"
            elif sell_signal:
                exit_price = price
                reason = "SELL_SIGNAL"

            if reason:
                gross = exit_price - self.entry_price
                fee = (self.entry_price + exit_price) * self.fee
                pnl = gross - fee

                self.realized_pnl += pnl
                self.position = 0
                self.trades += 1

                self.log(reason, exit_price, pnl)

                print(
                    f"💰 {reason} @ {exit_price:.2f} | "
                    f"TradePnL={pnl:.2f} | NetPnL={self.realized_pnl:.2f}"
                )

    def log(self, action, price, trade_pnl):
        with open("trade_log_bb_rsi_volume.csv", "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                action,
                round(price, 2),
                round(trade_pnl, 2),
                round(self.realized_pnl, 2)
            ])

    # -----------------------------
    # Runner
    # -----------------------------
    def run(self, duration=86400):
        print("\n🚀 Bot Running (Paper Trading)\n")
        end = time.time() + duration
        while time.time() < end:
            try:
                self.process()
                time.sleep(60)
            except Exception as e:
                print("⚠️ Error:", e)
                time.sleep(10)


# -----------------------------
# BOT CONFIG (FULL CONTROL)
# -----------------------------
bot = BB_RSI_Volume_PaperBot(
    api_key,
    api_secret,
    symbol="BTCUSDT",

    bb_window=39,
    bb_std=3,
    rsi_period=15,

    vol_window=27,
    vol_multiplier=1.5,
    trend_sma=49,

    tp_pct=0.07,
    sl_pct=0.03
)

bot.run(864000)
