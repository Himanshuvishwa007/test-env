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
                 bb_window=50, bb_std_dev=3, sma_filter_window=30,
                 taker_fee=0.001, tp_pct=0.02, sl_pct=0.01):

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
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

        with open("trade_log_bollinger.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Time", "Action", "Price", "TradePnL", "NetPnL", "Equity"])

        print(f"✅ Bot initialized: {symbol} | Interval=1m")
        print(f"⚙️ Params → BB Window={bb_window}, BB Std={bb_std_dev}, SMA Filter={sma_filter_window}, "
              f"Fee={taker_fee*100:.2f}%, TP={tp_pct*100:.1f}%, SL={sl_pct*100:.1f}%")

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

        # --- Take Profit / Stop Loss logic ---
        elif self.position == 1:
            tp_price = self.entry_price * (1 + self.tp_pct)
            sl_price = self.entry_price * (1 - self.sl_pct)

            if price >= tp_price:
                reason = "TAKE_PROFIT"
                exit_price = tp_price
            elif price <= sl_price:
                reason = "STOP_LOSS"
                exit_price = sl_price
            elif sell_signal:
                reason = "SELL_SIGNAL"
                exit_price = price
            else:
                reason = None

            if reason:
                gross_pnl = (exit_price - self.entry_price)
                total_fee = (self.entry_price + exit_price) * self.taker_fee
                trade_pnl = gross_pnl - total_fee
                self.realized_pnl += trade_pnl
                self.position = 0
                self.trades += 1
                print(f"💰 SELL ({reason}) @ {exit_price:.2f} | TradePnL={trade_pnl:.2f} | NetPnL={self.realized_pnl:.2f}")
                self.log_trade(reason, exit_price, trade_pnl, self.realized_pnl, self.equity_curve[-1])

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
        closes = df[df["Action"].isin(["SELL", "TAKE_PROFIT", "STOP_LOSS"])]
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
                time.sleep(60)
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
    bb_window=20,
    bb_std_dev=3,
    sma_filter_window=30,
    taker_fee=0.001,
    tp_pct=0.04,   # 2% take profit
    sl_pct=0.01    # 1% stop loss
)

bot.run(duration=86400)  # Run for 24 hours
