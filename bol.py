from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import numpy as np
import time
import csv
import json
import os
import math
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

# -----------------------------
# 🔑 API KEYS (replace with yours)
# -----------------------------
api_key = "cL4LHJfRXt9FO2p3f65-nlH2jihupQU8uHJEv1BSrC48P6YWSQJdUVgV6"
api_secret = "lkhKrYkoMIldhtabkdU-yCj7gD8GMYDILZ0vZg7iIZWEGuEE63JqjtpQG"

# -----------------------------
# ⚙️ TRADING CONFIG
# -----------------------------
SYMBOL = "BTCFDUSD"               # primary symbol
FALLBACK_SYMBOL = "BTCUSDT"       # fallback if primary fails repeatedly
INTERVAL_SECS = 60
BB_WINDOW = 41
BB_STD_DEV = 3
SMA_FILTER_WINDOW = 49
TP_PCT = 0.007
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
        self.position = 0
        self.entry_price = None
        self.realized_pnl = 0.0
        self.trades = 0
        self.prices = []

        self.load_state()
        self.detect_existing_position()

        if not os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Action", "Symbol", "Price", "TradePnL", "NetPnL"])

        print(f"✅ Live Bot initialized for {symbol}")
        print(f"⚙️ Params: BB Window={BB_WINDOW}, BB Std={BB_STD_DEV}, SMA Filter={SMA_FILTER_WINDOW}, TP={TP_PCT*100:.2f}%, SL={SL_PCT*100:.2f}%")

    # -----------------------------
    # 🔹 Helpers: Binance filters & qty adjustment
    # -----------------------------
    def _get_lot_size_filter(self, symbol):
        """Return (minQty as Decimal, stepSize as Decimal)."""
        info = self.client.get_symbol_info(symbol)
        if not info:
            raise Exception(f"No symbol info for {symbol}")
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                min_q = Decimal(str(f["minQty"]))
                step = Decimal(str(f["stepSize"]))
                return min_q, step
        # sensible fallback
        return Decimal("0.00000001"), Decimal("0.00000001")

    def _adjust_qty_down(self, qty_float, step_decimal, min_qty_decimal):
        """Round down qty to nearest step and ensure >= minQty, return Decimal or Decimal(0)."""
        qty = Decimal(str(qty_float))
        # quantize to step (round down)
        try:
            adjusted = (qty // step_decimal) * step_decimal
        except Exception:
            # fallback if division with decimals fails
            adjusted = (qty.quantize(step_decimal, rounding=ROUND_DOWN))
        if adjusted < min_qty_decimal:
            return Decimal("0")
        return adjusted

    # -----------------------------
    # 🔹 State Management
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
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.position = state.get("position", 0)
                self.entry_price = state.get("entry_price")
                self.realized_pnl = state.get("realized_pnl", 0.0)
                self.trades = state.get("trades", 0)
                print(f"📂 Loaded state → Pos={self.position}, Entry={self.entry_price}, PnL={self.realized_pnl:.2f}")
            except Exception as e:
                print(f"⚠️ Failed loading state: {e}")

    # -----------------------------
    # 🔹 Detect Existing Position
    # -----------------------------
    def detect_existing_position(self):
        try:
            fdusd, btc = self.get_balances()
        except Exception as e:
            print(f"⚠️ Could not get balances: {e}")
            return

        if btc > 0.000001:
            if self.position == 0:
                inferred = self._compute_entry_price_from_trades()
                self.entry_price = inferred
                self.position = 1
                print(f"⚠️ Existing BTC detected ({btc:.6f}). Entry price set to {self.entry_price or 'Unknown'}.")
            else:
                print(f"⚠️ BTC detected, position already marked. Entry={self.entry_price}")
        else:
            print("🟢 No BTC position detected. Starting fresh.")

    # -----------------------------
    # 🔹 Binance Helpers
    # -----------------------------
    def fetch_price(self, symbol=None):
        sym = symbol or self.symbol
        try:
            return float(self.client.get_symbol_ticker(symbol=sym)["price"])
        except Exception as e:
            print(f"⚠️ Price fetch error for {sym}: {e}")
            time.sleep(2)
            return None

    def get_balances(self):
        balances = self.client.get_account()["balances"]
        df = pd.DataFrame(balances)
        df.set_index("asset", inplace=True)
        fdusd = float(df.loc["FDUSD", "free"]) if "FDUSD" in df.index else 0.0
        btc = float(df.loc["BTC", "free"]) if "BTC" in df.index else 0.0
        return fdusd, btc

    def _compute_entry_price_from_trades(self):
        try:
            trades = self.client.get_my_trades(symbol=self.symbol)
            if not trades:
                return None
            total_buy_cost = total_buy_qty = total_sell_cost = total_sell_qty = 0.0
            for t in trades:
                qty = float(t["qty"])
                price = float(t["price"])
                if t.get("isBuyer"):
                    total_buy_cost += qty * price
                    total_buy_qty += qty
                else:
                    total_sell_cost += qty * price
                    total_sell_qty += qty
            net_qty = total_buy_qty - total_sell_qty
            if net_qty <= 0:
                return None
            net_cost = total_buy_cost - total_sell_cost
            if net_cost <= 0:
                return None
            return net_cost / net_qty
        except Exception as e:
            print(f"⚠️ Failed to infer entry price: {e}")
            return None

    # -----------------------------
    # 🔹 Trading Functions (safe)
    # -----------------------------
    def buy_all(self, symbol=None):
        sym = symbol or self.symbol
        try:
            fdusd, _ = self.get_balances()
            spend_amt = fdusd * (1 - CASH_RESERVE_PCT)
            if spend_amt < 5:
                print(f"⚠️ Not enough FDUSD to buy ({spend_amt:.2f}) on {sym}")
                return

            # use quoteOrderQty so Binance computes buy quantity
            order = self.client.order_market_buy(symbol=sym, quoteOrderQty=round(spend_amt, 2))
            fills = order.get("fills", [])
            qty = sum(float(f["qty"]) for f in fills)
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            self.entry_price = total_cost / qty if qty > 0 else self.fetch_price(sym)
            self.position = 1
            self.trades += 1
            print(f"📈 BUY ({sym}) @ {self.entry_price:.2f} | Qty={qty:.6f} | Spent≈{spend_amt:.2f}")
            self.log_trade("BUY", sym, self.entry_price)
        except BinanceAPIException as e:
            print(f"⚠️ Buy failed on {sym}: {e}")
        except Exception as e:
            print(f"⚠️ Buy unexpected error on {sym}: {e}")

    def sell_all(self, price, reason):
        """
        Attempt to sell on primary symbol, retry adjusting precision,
        on repeated failure try fallback symbol.
        """
        for attempt_symbol in [self.symbol, FALLBACK_SYMBOL]:
            successful = self._attempt_sell_with_retries(attempt_symbol, price, reason)
            if successful:
                return True
        # if reached here, none succeeded
        print("⚠️ Sell failed on both primary and fallback symbols.")
        return False

    def _attempt_sell_with_retries(self, symbol, price, reason):
        """
        Try to sell with progressive retries adjusting quantity down by step.
        Returns True if sold, False otherwise.
        """
        try:
            # get current BTC balance (base asset)
            base_asset = "BTC"
            bal_info = self.client.get_asset_balance(asset=base_asset)
            balance = float(bal_info["free"]) if bal_info and bal_info.get("free") else 0.0
            if balance <= 0:
                print(f"⚠️ No {base_asset} balance to sell on {symbol}")
                return False

            min_qty, step = self._get_lot_size_filter(symbol)
            # apply base reserve
            qty_decimal = Decimal(str(balance)) * (Decimal("1") - Decimal(str(BASE_RESERVE_PCT)))
            qty_decimal = self._adjust_qty_decimal(qty_decimal, step, min_qty)
            if qty_decimal == Decimal("0"):
                print(f"⚠️ Qty below min for {symbol} after adjustment; balance={balance}")
                return False

            max_retries = 6
            for attempt in range(max_retries):
                try_qty = qty_decimal - (step * Decimal(attempt))  # reduce by steps each retry
                if try_qty < min_qty:
                    print(f"⚠️ try_qty {try_qty} < min_qty for {symbol}, stop retrying.")
                    break
                # convert to string to avoid float precision issues
                q_str = format(try_qty.normalize(), 'f')
                # place order
                order = self.client.order_market_sell(symbol=symbol, quantity=q_str)
                exec_qty = 0.0
                if "executedQty" in order:
                    exec_qty = float(order["executedQty"])
                elif "fills" in order:
                    exec_qty = sum(float(f["qty"]) for f in order["fills"])
                trade_pnl = (price - float(self.entry_price)) * exec_qty if self.entry_price else 0.0

                # update state
                self.position = 0
                self.trades += 1
                self.realized_pnl += trade_pnl
                print(f"💰 SELL ({reason}) on {symbol} @ {price:.2f} | Qty={exec_qty:.6f} | PnL={trade_pnl:.2f} | NetPnL={self.realized_pnl:.2f}")
                self.log_trade(reason, symbol, price, trade_pnl, self.realized_pnl)
                self.save_state()
                return True

            # if loop finishes without return, we failed to place sell
            return False

        except BinanceAPIException as e:
            # handle common Binance API errors explicitly
            code = None
            try:
                code = e.code
            except Exception:
                pass
            msg = str(e)
            print(f"⚠️ Sell BinanceAPIException on {symbol}: {msg}")
            # if insufficient balance, try re-sync and reduce qty then retry once
            if code == -2010 or "insufficient balance" in msg.lower():
                print("🔁 Detected insufficient balance. Re-fetching balance and retrying once.")
                try:
                    time.sleep(0.5)
                    bal_info = self.client.get_asset_balance(asset="BTC")
                    balance = float(bal_info["free"]) if bal_info and bal_info.get("free") else 0.0
                    min_qty, step = self._get_lot_size_filter(symbol)
                    qty_decimal = Decimal(str(balance)) * (Decimal("1") - Decimal(str(BASE_RESERVE_PCT)))
                    qty_decimal = self._adjust_qty_decimal(qty_decimal, step, min_qty)
                    if qty_decimal > Decimal("0"):
                        try_qty_str = format(qty_decimal.normalize(), 'f')
                        order = self.client.order_market_sell(symbol=symbol, quantity=try_qty_str)
                        exec_qty = float(order.get("executedQty") or 0.0)
                        trade_pnl = (price - float(self.entry_price)) * exec_qty if self.entry_price else 0.0
                        self.position = 0
                        self.trades += 1
                        self.realized_pnl += trade_pnl
                        print(f"💰 SELL (retry) ({reason}) on {symbol} @ {price:.2f} | Qty={exec_qty:.6f} | PnL={trade_pnl:.2f}")
                        self.log_trade(reason, symbol, price, trade_pnl, self.realized_pnl)
                        self.save_state()
                        return True
                except Exception as ex:
                    print(f"⚠️ Retry sell also failed: {ex}")
            # if precision/lot errors, we'll return False so fallback symbol can be tried
            return False
        except Exception as e:
            print(f"⚠️ Unexpected error during sell attempt on {symbol}: {e}")
            return False

    def _adjust_qty_decimal(self, qty_decimal, step_decimal, min_qty_decimal):
        """Return adjusted Decimal quantity rounded down to step, or 0 if below min."""
        if qty_decimal <= 0:
            return Decimal("0")
        # floor to step: (qty // step) * step
        try:
            times = (qty_decimal // step_decimal)
            adjusted = times * step_decimal
        except Exception:
            # fallback using quantize
            adjusted = qty_decimal.quantize(step_decimal, rounding=ROUND_DOWN)
        if adjusted < min_qty_decimal:
            return Decimal("0")
        return adjusted

    # -----------------------------
    # 🔹 Strategy Logic
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

        unreal_pnl = 0.0
        if self.entry_price and self.position == 1:
            unreal_pnl = ((price - self.entry_price) / self.entry_price) * 100.0

        buy_signal = last["Close"] < last["Lower"] and last["Close"] > last["LongSMA"] * 0.8
        sell_signal = last["Close"] > last["SMA"]

        if buy_signal and self.position == 0:
            self.buy_all()

        elif self.position == 1 and self.entry_price:
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
                sold = self.sell_all(price, reason)
                if not sold:
                    # if sell fails, resync and continue (so bot can attempt later)
                    print("⚠️ Sell attempt failed; resyncing balances and will retry on next loop.")
                    self.detect_existing_position()

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Price={price:.2f} | Pos={self.position} | UnrealPnL={unreal_pnl:.2f}% | NetPnL={self.realized_pnl:.2f} | Trades={self.trades}")

    # -----------------------------
    # 🔹 Logging
    # -----------------------------
    def log_trade(self, action, symbol, price, trade_pnl=0, net_pnl=0):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([now, action, symbol, price, trade_pnl, net_pnl])

    # -----------------------------
    # 🔹 Run Loop
    # -----------------------------
    def run(self):
        print(f"\n🚀 Running LIVE BOT for {self.symbol}")
        print("Press Ctrl + C to stop.\n")
        while True:
            try:
                price = self.fetch_price(self.symbol)
                self.process_new_price(price)
                time.sleep(INTERVAL_SECS)
            except KeyboardInterrupt:
                print("\n🛑 Bot stopped manually.")
                self.save_state()
                break
            except Exception as e:
                with open(ERROR_LOG, "a") as f:
                    f.write(f"{datetime.now(timezone.utc)} - {e}\n")
                print(f"⚠️ Runtime error: {e}")
                time.sleep(5)

# -----------------------------
# 🚀 START BOT
# -----------------------------
if __name__ == "__main__":
    bot = BollingerSpotLive(api_key, api_secret, SYMBOL)
    bot.run()
