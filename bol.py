from binance.client import Client
import pandas as pd
import numpy as np
import time
import csv
from datetime import datetime

# -----------------------------
# Connect to Binance Testnet
# -----------------------------
api_key = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
api_secret = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"
client = Client(api_key, api_secret, testnet=True)


class BollingerSpotBot:
    def __init__(self, api_key, api_secret, symbol="BTCUSDT", initial_cash=10000,
                 bb_window=50, bb_std_dev=3, sma_filter_window=30, taker_fee=0.001):
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
        self.bb_window = bb_window
        self.bb_std_dev = bb_std_dev
        self.sma_filter_window = sma_filter_window
        self.taker_fee = taker_fee

        # Create trade log
        with open("trade_log_bollinger.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Time", "Action", "Price", "TradePnL", "NetPnL", "Equity"])

        print(f"✅ Bot initialized: {symbol} | Interval=1m")
        print(f"⚙️  Params → BB Window={bb_window}, BB Std={bb_std_dev}, SMA Filter={sma_filter_window}, Fee={taker_fee*100:.2f}%")

    def log_trade(self, action, price, trade_pnl, net_pnl, equity):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with open("trade_log_bollinger.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([now, action, price, trade_pnl, net_pnl, equity])

    def process_new_price(self, price):
        self.prices.append(price)
        if len(self.prices) < max(self.bb_window, self.sma_filter_window):
            return

        df = pd.DataFrame({"Close": self.prices})
        df["SMA"] = df["Close"].rolling(self.bb_window).mean()
        df["STD"] = df["Close"].rolling(self.bb_window).std()
        df["Upper"] = df["SMA"] + (self.bb_std_dev * df["STD"])
        df["Lower"] = df["SMA"] - (self.bb_std_dev * df["STD"])
        df["LongSMA"] = df["Close"].rolling(self.sma_filter_window).mean()
        df.dropna(inplace=True)
        last = df.iloc[-1]

        # --- Bollinger Band + SMA logic ---
        buy_signal = last["Close"] < last["Lower"] and last["Close"] > last["LongSMA"] * 0.8
        sell_signal = last["Close"] > last["SMA"]

        # --- Execute Trades ---
        if buy_signal and self.position == 0:
            self.entry_price = price
            self.position = 1
            self.trades += 1
            print(f"📈 BUY @ {price:.2f}")
            self.log_trade("BUY", price, 0, self.realized_pnl, self.equity_curve[-1])

        elif sell_signal and self.position == 1:
            gross_pnl = (price - self.entry_price)
            total_fee = (self.entry_price + price) * self.taker_fee
            trade_pnl = gross_pnl - total_fee
            self.realized_pnl += trade_pnl
            self.position = 0
            self.trades += 1
            print(f"💰 SELL @ {price:.2f} | TradePnL={trade_pnl:.2f} | NetPnL={self.realized_pnl:.2f}")
            self.log_trade("SELL", price, trade_pnl, self.realized_pnl, self.equity_curve[-1])

        # --- Update Equity ---
        if self.position == 1:
            unrealized_pnl = (price - self.entry_price) - (price * self.taker_fee)
        else:
            unrealized_pnl = 0

        equity = self.initial_cash + self.realized_pnl + unrealized_pnl
        self.equity_curve.append(equity)

        print(f"[Price={price:.2f}] Pos={self.position} | Unr.PnL={unrealized_pnl:.2f} | "
              f"NetPnL={self.realized_pnl:.2f} | Equity={equity:.2f} | Trades={self.trades}")

    def performance_report(self):
        df = pd.read_csv("trade_log_bollinger.csv")
        closes = df[df["Action"].isin(["SELL"])]
        total_trades = len(closes)
        wins = len(closes[closes["TradePnL"] > 0])
        losses = len(closes[closes["TradePnL"] <= 0])
        win_rate = (wins / total_trades * 100) if total_trades else 0
        total_pnl = df["NetPnL"].iloc[-1] if len(df) else 0
        final_equity = df["Equity"].iloc[-1] if len(df) else self.initial_cash
        profit_pct = (final_equity - self.initial_cash) / self.initial_cash * 100

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
        print(f"Taker Fee: {self.taker_fee * 100:.2f}% per trade")
        print("--------------------------------\n")

    def run(self, duration=86400):
        end_time = time.time() + duration
        print(f"\n🚀 Starting BollingerSpotBot for {self.symbol} | Interval=1m | Runtime={duration/3600:.1f}h\n")
        while time.time() < end_time:
            try:
                price = float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])
                self.process_new_price(price)
                time.sleep(60)  # 1-minute delay for each update
            except Exception as e:
                print(f"⚠️ Error: {e}")
                time.sleep(10)
        self.performance_report()


# -----------------------------
# Run the Bot
# -----------------------------
bot = BollingerSpotBot(
    api_key,
    api_secret,
    symbol="BTCUSDT",
    initial_cash=10000,
    bb_window=50,
    bb_std_dev=3,
    sma_filter_window=30,
    taker_fee=0.001
)

bot.run(duration=86400)  # Run for 24 hours
