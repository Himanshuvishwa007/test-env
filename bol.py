from binance.client import Client
import pandas as pd
import numpy as np
import time
import csv
import json
import os
from datetime import datetime

# -----------------------------
# 🔑 API KEYS (replace with yours)
# -----------------------------
api_key = "cL4LHJfRXt9FO2p3f65GGvqBmypnlH2jihupQU8uHJEv1BSrC48P6YWSQJdUVgV6"
api_secret = "lkhKrYkoMIldhtabkdU1KyiUi2WyCj7gD8GMYDILZ0vZg7iIZWEGuEE63JqjtpQG"

# -----------------------------
# ⚙️ TRADING CONFIG
# -----------------------------
SYMBOL = "BTCFDUSD"
INTERVAL_SECS = 60
BB_WINDOW = 41
BB_STD_DEV = 3
SMA_FILTER_WINDOW = 49
TP_PCT = 0.07
SL_PCT = 0.03
CASH_RESERVE_PCT = 0.001
BASE_RESERVE_PCT = 0.001

STATE_FILE = "trade_state.json"
TRADE_LOG = "trade_log_live.csv"
ERROR_LOG = "error_log.txt"

# -----------------------------
# 🧠 BOT CLASS
# -----------------------------
class BollingerSpotLive:
    def __init__(self, api_key, api_secret, symbol):
        self.client = Client(api_key, api_secret)
        self.symbol = symbol

        # Default values
        self.position = 0
        self.entry_price = None
        self.realized_pnl = 0.0
        self.trades = 0
        self.prices = []

        # Try loading previous trade state
        self.load_state()

        # Detect current live position from Binance
        self.detect_existing_position()

        # Initialize trade log file if not exist
        if not os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Action", "Price", "TradePnL", "NetPnL"])

        print(f"✅ Live Bot initialized for {symbol}")
        print(f"⚙️ Params: BB Window={BB_WINDOW}, BB Std={BB_STD_DEV}, SMA Filter={SMA_FILTER_WINDOW}, "
              f"TP={TP_PCT*100:.1f}%, SL={SL_PCT*100:.1f}%")

    # -----------------------------
    # 🔹 State Persistence
    # -----------------------------
    def save_state(self):
        state = {
            "position": self.position,
            "entry_price": self.entry_price,
            "realized_pnl": self.realized_pnl,
            "trades": self.trades
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    def load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                self.position = state.get("position", 0)
                self.entry_price = state.get("entry_price")
                self.realized_pnl = state.get("realized_pnl", 0.0)
                self.trades = state.get("trades", 0)
                print(f"📂 Loaded previous trade state → Pos={self.position}, Entry={self.entry_price}, PnL={self.realized_pnl:.2f}")

    # -----------------------------
    # 🔹 Detect existing Binance position
    # -----------------------------
    def detect_existing_position(self):
        fdusd, btc = self.get_balances()
        if btc > 0.0001:
            if self.position == 0:
                self.position = 1
                self.entry_price = float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])
                print(f"⚠️ Existing BTC detected ({btc:.6f}). Assuming open LONG position @ ~{self.entry_price:.2f}")
        else:
            print("🟢 No BTC position detected. Starting fresh.")

    # -----------------------------
    # 🔹 Utility Functions
    # -----------------------------
    def log_trade(self, action, price, trade_pnl=0, net_pnl=0):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([now, action, price, trade_pnl, net_pnl])
        self.save_state()

    def fetch_price(self):
        try:
            price = float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])
            return price
        except Exception as e:
            print(f"⚠️ Price fetch error: {e}")
            time.sleep(5)
            return None

    def get_balances(self):
        balances = self.client.get_account()["balances"]
        df = pd.DataFrame(balances)
        df.set_index("asset", inplace=True)
        fdusd = float(df.loc["FDUSD", "free"]) if "FDUSD" in df.index else 0
        btc = float(df.loc["BTC", "free"]) if "BTC" in df.index else 0
        return fdusd, btc

    # -----------------------------
    # 🔹 Trade Functions
    # -----------------------------
    def buy_all(self, price):
        fdusd, _ = self.get_balances()
        spend_amt = fdusd * (1 - CASH_RESERVE_PCT)
        if spend_amt < 5:
            print(f"⚠️ Not enough FDUSD to buy ({spend_amt:.2f}).")
            return

        try:
            order = self.client.order_market_buy(symbol=self.symbol, quoteOrderQty=round(spend_amt, 2))
            qty = sum(float(o["qty"]) for o in order["fills"])
            self.entry_price = price
            self.position = 1
            self.trades += 1
            print(f"📈 BUY @ {price:.2f} | Qty={qty:.6f} | Spent={spend_amt:.2f}")
            self.log_trade("BUY", price)
        except Exception as e:
            print(f"⚠️ Buy order failed: {e}")

    def sell_all(self, price, reason):
        _, btc = self.get_balances()
        qty = btc * (1 - BASE_RESERVE_PCT)
        if qty <= 0:
            print("⚠️ No BTC available to sell.")
            return

        try:
            order = self.client.order_market_sell(symbol=self.symbol, quantity=round(qty, 6))
            self.position = 0
            self.trades += 1
            trade_pnl = (price - self.entry_price) * qty
            self.realized_pnl += trade_pnl
            print(f"💰 SELL ({reason}) @ {price:.2f} | Qty={qty:.6f} | TradePnL={trade_pnl:.2f} | NetPnL={self.realized_pnl:.2f}")
            self.log_trade(reason, price, trade_pnl, self.realized_pnl)
            self.save_state()
        except Exception as e:
            print(f"⚠️ Sell order failed: {e}")

    # -----------------------------
    # 🔹 Bollinger Strategy Logic
    # -----------------------------
    def process_new_price(self, price):
        if price is None:
            return

        self.prices.append(price)
        if len(self.prices) < max(BB_WINDOW, SMA_FILTER_WINDOW):
            return

        df = pd.DataFrame({"Close": self.prices})
        df["SMA"] = df["Close"].rolling(BB_WINDOW).mean()
        df["STD"] = df["Close"].rolling(BB_WINDOW).std()
        df["Upper"] = df["SMA"] + (BB_STD_DEV * df["STD"])
        df["Lower"] = df["SMA"] - (BB_STD_DEV * df["STD"])
        df["LongSMA"] = df["Close"].rolling(SMA_FILTER_WINDOW).mean()
        df.dropna(inplace=True)
        last = df.iloc[-1]

        buy_signal = last["Close"] < last["Lower"] and last["Close"] > last["LongSMA"] * 0.8
        sell_signal = last["Close"] > last["SMA"]

        # --- BUY ---
        if buy_signal and self.position == 0:
            self.buy_all(price)

        # --- SELL ---
        elif self.position == 1:
            tp_price = self.entry_price * (1 + TP_PCT)
            sl_price = self.entry_price * (1 - SL_PCT)
            reason = None

            if price >= tp_price:
                reason = "TAKE_PROFIT"
            elif price <= sl_price:
                reason = "STOP_LOSS"
            elif sell_signal:
                reason = "SELL_SIGNAL"

            if reason:
                self.sell_all(price, reason)

        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Price={price:.2f} | Pos={self.position} | "
              f"NetPnL={self.realized_pnl:.2f} | Trades={self.trades}")

    # -----------------------------
    # 🔹 Reports & Loop
    # -----------------------------
    def performance_report(self):
        try:
            df = pd.read_csv(TRADE_LOG)
            closes = df[df["Action"].str.contains("SELL")]
            total_trades = len(closes)
            wins = len(closes[closes["TradePnL"] > 0])
            win_rate = (wins / total_trades * 100) if total_trades else 0
            total_pnl = df["NetPnL"].iloc[-1] if len(df) else 0
            print("\n📊 ---- PERFORMANCE SUMMARY ----")
            print(f"Total Trades: {total_trades}")
            print(f"Win Rate: {win_rate:.2f}%")
            print(f"Total P&L: {total_pnl:.2f}")
            print(f"Symbol: {self.symbol}")
            print("-------------------------------\n")
        except Exception as e:
            print(f"⚠️ Could not generate performance report: {e}")

    def run(self):
        print(f"\n🚀 Running LIVE BOT for {self.symbol}")
        print("Press Ctrl + C to stop anytime.\n")

        while True:
            try:
                price = self.fetch_price()
                self.process_new_price(price)
                time.sleep(INTERVAL_SECS)

            except KeyboardInterrupt:
                print("\n🛑 Bot stopped manually.")
                self.performance_report()
                self.save_state()
                break

            except Exception as e:
                print(f"⚠️ Runtime Error: {e}")
                with open(ERROR_LOG, "a") as f:
                    f.write(f"{datetime.utcnow()} - {e}\n")
                time.sleep(5)
                continue


# -----------------------------
# 🚀 START BOT
# -----------------------------
if __name__ == "__main__":
    bot = BollingerSpotLive(api_key, api_secret, SYMBOL)
    bot.run()
