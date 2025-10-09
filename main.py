from binance.client import Client
import pandas as pd
import time
import numpy as np
import csv
from datetime import datetime

# -----------------------------
# Connect to Binance Testnet
# -----------------------------
api_key = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
api_secret = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"
client = Client(api_key, api_secret, testnet=True)


class RealTimePaperTrading:
    def __init__(self, api_key, api_secret, symbol="BTCUSDT", initial_cash=10000, threshold=0.004,
                 momentum_window=20, meanrev_window=50):
        self.client = Client(api_key, api_secret, testnet=True)
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.position = 0
        self.entry_price = None
        self.trades = 0
        self.realized_pnl = 0.0
        self.equity_curve = [initial_cash]
        self.prices = []
        self.threshold = threshold
        self.momentum_window = momentum_window
        self.meanrev_window = meanrev_window
        self.trade_log = []  # store trade history

        # Prepare CSV for trade logs
        with open("trade_log.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Time", "Action", "Price", "P&L", "Net_P&L", "Equity"])

    def log_trade(self, action, price, trade_pnl, net_pnl, equity):
        """Log each trade to CSV."""
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with open("trade_log.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([now, action, price, trade_pnl, net_pnl, equity])

    def process_new_price(self, price):
        """Handle new tick/price every 5 seconds."""
        self.prices.append(price)
        if len(self.prices) < self.meanrev_window:
            return

        df = pd.DataFrame({"Close": self.prices})
        df["Return"] = np.log(df["Close"] / df["Close"].shift(1))
        df["Momentum"] = df["Return"].rolling(self.momentum_window).mean()
        df["MeanRev_MA"] = df["Close"].rolling(self.meanrev_window).mean()
        df.dropna(inplace=True)

        if df.empty:
            return

        last_row = df.iloc[-1]
        momentum_signal = 1 if last_row["Momentum"] > 0 else -1
        meanrev_signal = 0
        if last_row["Close"] < last_row["MeanRev_MA"] * (1 - self.threshold):
            meanrev_signal = 1
        elif last_row["Close"] > last_row["MeanRev_MA"] * (1 + self.threshold):
            meanrev_signal = -1

        combined_signal = momentum_signal + meanrev_signal
        desired_position = 1 if combined_signal > 0 else -1 if combined_signal < 0 else 0

        # --- Trade Execution ---
        if desired_position != self.position:
            if self.position != 0:  # closing previous trade
                trade_pnl = (price - self.entry_price) * self.position
                self.realized_pnl += trade_pnl
                print(f"💰 Trade Closed | P&L={trade_pnl:.2f} | Net P&L={self.realized_pnl:.2f}")
                self.log_trade("CLOSE", price, trade_pnl, self.realized_pnl, self.equity_curve[-1])

            if desired_position != 0:  # opening new trade
                self.entry_price = price
                print(f"📈 New Trade Opened | Position={desired_position} @ {price:.2f}")
                self.log_trade("OPEN", price, 0, self.realized_pnl, self.equity_curve[-1])

            self.trades += 1
            self.position = desired_position

        # Update equity
        if self.position != 0 and self.entry_price is not None:
            unrealized_pnl = (price - self.entry_price) * self.position
        else:
            unrealized_pnl = 0

        equity = self.initial_cash + self.realized_pnl + unrealized_pnl
        self.equity_curve.append(equity)

        print(f"[Price={price:.2f}] Position={self.position} | Unrealized P&L={unrealized_pnl:.2f} | "
              f"Net P&L={self.realized_pnl:.2f} | Equity={equity:.2f} | Trades={self.trades}")

    def performance_report(self):
        """Generate performance stats from logs."""
        df = pd.read_csv("trade_log.csv")
        closes = df[df["Action"] == "CLOSE"]
        total_trades = len(closes)
        wins = len(closes[closes["P&L"] > 0])
        losses = len(closes[closes["P&L"] <= 0])
        win_rate = (wins / total_trades * 100) if total_trades else 0
        total_pnl = df["Net_P&L"].iloc[-1] if len(df) else 0
        final_equity = df["Equity"].iloc[-1] if len(df) else self.initial_cash
        profit_pct = (final_equity - self.initial_cash) / self.initial_cash * 100

        # Max drawdown
        if len(df) > 1:
            equity = df["Equity"]
            rolling_max = equity.cummax()
            drawdown = (equity - rolling_max) / rolling_max
            max_dd = drawdown.min() * 100
        else:
            max_dd = 0

        print("\n📊 ---- PERFORMANCE SUMMARY ----")
        print(f"Total Trades: {total_trades}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Total P&L: {total_pnl:.2f}")
        print(f"Profit %: {profit_pct:.2f}%")
        print(f"Max Drawdown: {max_dd:.2f}%")
        print("--------------------------------\n")

    def run(self, duration=86400):
        """Fetch live prices every 5 seconds for `duration` seconds."""
        end_time = time.time() + duration
        while time.time() < end_time:
            try:
                price = float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])
                self.process_new_price(price)
                time.sleep(5)
            except Exception as e:
                print(f"⚠️ Error: {e}")
                time.sleep(10)

        self.performance_report()


# -----------------------------
# Run for 24 hours
# -----------------------------
bot = RealTimePaperTrading(
    api_key,
    api_secret,
    symbol="BTCUSDT",
    initial_cash=10000,
    threshold=0.003,
    momentum_window=10,
    meanrev_window=30
)

bot.run(duration=86400)  # 24 hours
