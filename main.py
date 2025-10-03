from binance.client import Client
import pandas as pd
import time
import numpy as np

# -----------------------------
# Connect to Binance Testnet
# -----------------------------
api_key = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
api_secret = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"
client = Client(api_key, api_secret, testnet=True)


class RealTimePaperTrading:
    def __init__(self, api_key, api_secret, symbol="BTCUSDT", initial_cash=10000, threshold=0.004, momentum_window=20, meanrev_window=50):
        self.client = Client(api_key, api_secret, testnet=True)
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.position = 0   # -1 = short, 0 = flat, +1 = long
        self.entry_price = None
        self.trades = 0
        self.realized_pnl = 0.0
        self.equity_curve = [initial_cash]
        self.prices = []
        self.threshold = threshold
        self.momentum_window = momentum_window
        self.meanrev_window = meanrev_window

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

            if desired_position != 0:  # opening new trade
                self.entry_price = price
                print(f"📈 New Trade Opened | Position={desired_position} @ {price:.2f}")

            self.trades += 1
            self.position = desired_position

        # Update equity
        if self.position != 0 and self.entry_price is not None:
            unrealized_pnl = (price - self.entry_price) * self.position
        else:
            unrealized_pnl = 0

        equity = self.initial_cash + self.realized_pnl + unrealized_pnl
        self.equity_curve.append(equity)

        print(f"[Price={price:.2f}] Position={self.position} | Unrealized P&L={unrealized_pnl:.2f} | Net P&L={self.realized_pnl:.2f} | Equity={equity:.2f} | Trades={self.trades}")

    def run(self, duration=300):
        """Fetch live prices every 5 seconds for `duration` seconds."""
        end_time = time.time() + duration
        while time.time() < end_time:
            price = float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])  # last price
            self.process_new_price(price)
            time.sleep(5)  # <-- 5 second interval

        final_perf = (self.equity_curve[-1] - self.initial_cash) / self.initial_cash * 100
        print(f"\n✅ Final Performance: {final_perf:.2f}% | Net P&L={self.realized_pnl:.2f} | Trades={self.trades}")

bot = RealTimePaperTrading(
    api_key,
    api_secret,
    symbol="BTCUSDT",
    initial_cash=10000,
    threshold=0.004,
    momentum_window=20,
    meanrev_window=50
)

bot.run(duration=7200)  # run for 5 minutes (300 sec) with 5s updates