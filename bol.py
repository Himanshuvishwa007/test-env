#!/usr/bin/env python3
"""
bol.py - Bollinger Spot bot (Strict Ignore mode)

Strict Ignore: if an order would be dust (qty < minQty or notional < minNotional),
the bot will SKIP that buy/sell (no auto-adjust), and keep working safely.
"""

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
# API KEYS (prefer env vars)
# -----------------------------
API_KEY = "cL4LHJfRXt9FO2p3f65GGvqBmypnlH2jihupQU8uHJEv1BSrC48P6YWSQJdUVgV6"
API_SECRET = "lkhKrYkoMIldhtabkdU1KyiUi2WyCj7gD8GMYDILZ0vZg7iIZWEGuEE63JqjtpQG"

# -----------------------------
# CONFIG
# -----------------------------
PRIMARY_SYMBOL = "BTCFDUSD"
FALLBACK_SYMBOL = "BTCUSDT"
SYMBOL = PRIMARY_SYMBOL        # strategy uses primary symbol for price / calculation
INTERVAL_SECS = 60
BB_WINDOW = 20
BB_STD_DEV = 3
SMA_FILTER_WINDOW = 30
TP_PCT = 0.07        
SL_PCT = 0.01         
CASH_RESERVE_PCT = 0.001
BASE_RESERVE_PCT = 0.001

STATE_FILE = "trade_state.json"
TRADE_LOG = "trade_log_live.csv"
ERROR_LOG = "error_log.txt"

API_MAX_RETRIES = 4
API_BACKOFF_BASE = 0.5

# -----------------------------
# Helper: retry wrapper
# -----------------------------
def api_retry(fn, *args, **kwargs):
    backoff = API_BACKOFF_BASE
    for attempt in range(API_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException:
            # let caller handle Binance errors specifically
            raise
        except Exception as e:
            if attempt < API_MAX_RETRIES - 1:
                print(f"⚠️ Transient error: {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                raise

# -----------------------------
# Bot
# -----------------------------
class BollingerStrictIgnore:
    def __init__(self, api_key, api_secret, symbol):
        self.client = Client(api_key, api_secret)
        self.symbol = symbol
        self.position = 0            # 0 = no, 1 = long
        self.entry_price = None
        self.realized_pnl = 0.0
        self.trades = 0
        self.prices = []

        self._symbol_info_cache = {}

        # create log file
        if not os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Action", "Symbol", "Price", "TradePnL", "NetPnL"])

        self.load_state()
        self.sync_server_time()
        self.detect_existing_position()
        print(f"✅ Bot initialized for {self.symbol}")

    # --------------------
    # server time check
    # --------------------
    def sync_server_time(self):
        try:
            srv = api_retry(self.client.get_server_time)
            if srv and 'serverTime' in srv:
                pass
        except Exception as e:
            print(f"⚠️ Could not sync server time: {e}")

    # --------------------
    # symbol info / filters
    # --------------------
    def _get_symbol_info(self, symbol):
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        info = api_retry(self.client.get_symbol_info, symbol)
        self._symbol_info_cache[symbol] = info
        return info

    def get_filter(self, symbol, filter_type):
        info = self._get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f.get("filterType") == filter_type:
                return f
        return {}

    def get_lot_size(self, symbol):
        lot = self.get_filter(symbol, "LOT_SIZE")
        minQty = Decimal(str(lot.get("minQty", "0.00000001")))
        stepSize = Decimal(str(lot.get("stepSize", "0.00000001")))
        return minQty, stepSize

    def get_min_notional(self, symbol):
        f = self.get_filter(symbol, "MIN_NOTIONAL")
        if f and f.get("minNotional"):
            return Decimal(str(f["minNotional"]))
        # fallback to 1 (very small)
        return Decimal("1")

    # --------------------
    # state persistence
    # --------------------
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
                    st = json.load(f)
                self.position = st.get("position", 0)
                self.entry_price = st.get("entry_price")
                self.realized_pnl = st.get("realized_pnl", 0.0)
                self.trades = st.get("trades", 0)
                print(f"📂 Loaded state: pos={self.position}, entry={self.entry_price}, pnl={self.realized_pnl}")
            except Exception as e:
                print(f"⚠️ Could not load state: {e}")

    # --------------------
    # balance helpers
    # --------------------
    def get_balances(self):
        resp = api_retry(self.client.get_account)
        balances = resp.get("balances", [])
        df = pd.DataFrame(balances)
        df.set_index("asset", inplace=True)
        fdusd = float(df.loc["FDUSD", "free"]) if "FDUSD" in df.index else 0.0
        btc = float(df.loc["BTC", "free"]) if "BTC" in df.index else 0.0
        return fdusd, btc

    # --------------------
    # detect existing pos with strict dust check
    # --------------------
    def detect_existing_position(self):
        try:
            fdusd, btc = self.get_balances()
        except Exception as e:
            print(f"⚠️ Could not get balances: {e}")
            return

        # get minQty for symbol (use primary symbol)
        min_qty, _ = self.get_lot_size(self.symbol)
        # If BTC balance < minQty => treat as dust and reset state
        if Decimal(str(btc)) < min_qty:
            # reset internal state — strict ignore of dust
            if self.position != 0:
                print(f"🟡 Dust BTC detected ({btc:.8f}) < minQty ({min_qty}). Resetting internal position.")
                self.position = 0
                self.entry_price = None
                self.save_state()
            else:
                print(f"🟢 No tradable BTC (dust {btc:.8f}) — starting fresh.")
            return

        # If balance >= minQty and position not set, try to infer entry price
        if btc >= min_qty and self.position == 0:
            inferred = self._compute_entry_price_from_trades()
            self.entry_price = inferred
            self.position = 1
            print(f"⚠️ Existing BTC detected ({btc:.6f}). Inferred entry price: {self.entry_price}")
        elif btc >= min_qty:
            print(f"⚠️ BTC detected, position already marked. Entry={self.entry_price}")

    def _compute_entry_price_from_trades(self):
        try:
            trades = api_retry(self.client.get_my_trades, symbol=self.symbol)
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
            print(f"⚠️ Could not compute entry price: {e}")
            return None

    # --------------------
    # Bollinger helpers
    # --------------------
    def fetch_price(self, symbol=None):
        sym = symbol or self.symbol
        try:
            t = api_retry(self.client.get_symbol_ticker, symbol=sym)
            return float(t["price"])
        except Exception as e:
            print(f"⚠️ Price fetch error for {sym}: {e}")
            return None

    # --------------------
    # Strategy core
    # --------------------
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
        # compute unreal pnl only when position held and entry known
        if self.entry_price and self.position == 1:
            unreal_pnl = ((price - self.entry_price) / self.entry_price) * 100.0

        buy_signal = last["Close"] < last["Lower"] and last["Close"] > last["LongSMA"] * 0.8
        sell_signal = last["Close"] > last["SMA"]

        # BUY logic (strict)
        if buy_signal and self.position == 0:
            print("🔵 BUY SIGNAL (strict mode)")
            self.try_buy_strict(price)

        # SELL logic (strict)
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
                print(f"🔴 SELL TRIGGER ({reason}) - attempting strict sell")
                sold = self.try_sell_strict(price, reason)
                if not sold:
                    # strict ignore: if sell skipped due to dust, we reset and skip
                    print("🟡 Strict sell skipped (likely dust); resyncing positions.")
                    self.detect_existing_position()

        # print status
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Price={price:.2f} | Pos={self.position} | UnrealPnL={unreal_pnl:.2f}% | NetPnL={self.realized_pnl:.2f} | Trades={self.trades}")

    # --------------------
    # BUY - strict: skip if dust
    # --------------------
    def try_buy_strict(self, current_price):
        # determine spend amount in FDUSD
        try:
            fdusd, _ = self.get_balances()
        except Exception as e:
            print(f"⚠️ Balance fetch failed before buy: {e}")
            return False

        spend = Decimal(str(fdusd)) * (Decimal("1") - Decimal(str(CASH_RESERVE_PCT)))
        if spend < Decimal("5"):
            print(f"⚠️ Not enough FDUSD to buy (spend {spend}) - skipping buy")
            return False

        # estimate qty = spend / price
        est_qty = (spend / Decimal(str(current_price)))
        min_qty, step = self.get_lot_size(self.symbol)
        min_notional = self.get_min_notional(self.symbol)

        # strict check: if estimated qty < minQty OR est_qty*price < minNotional => skip
        if est_qty < min_qty:
            print(f"🟡 BUY SKIPPED — estimated qty {est_qty} < minQty {min_qty} (strict).")
            return False

        notional = est_qty * Decimal(str(current_price))
        if notional < min_notional:
            print(f"🟡 BUY SKIPPED — estimated notional {notional} < minNotional {min_notional} (strict).")
            return False

        # If checks passed, place market buy with quoteOrderQty so Binance computes qty
        try:
            self.sync_server_time()
            order = api_retry(self.client.order_market_buy, symbol=self.symbol, quoteOrderQty=float(round(float(spend), 2)))
            fills = order.get("fills", [])
            qty = sum(float(f["qty"]) for f in fills) if fills else 0.0
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills) if fills else 0.0
            if qty <= 0:
                print("⚠️ Buy executed but qty=0 — skipping")
                return False
            self.entry_price = total_cost / qty if total_cost and qty else float(current_price)
            self.position = 1
            self.trades += 1
            print(f"✅ BUY executed @ {self.entry_price:.2f} | Qty={qty:.6f}")
            self.log_trade("BUY", self.symbol, self.entry_price)
            self.save_state()
            return True
        except BinanceAPIException as e:
            print(f"⚠️ Buy BinanceAPIException: {e}")
            return False
        except Exception as e:
            print(f"⚠️ Buy failed: {e}")
            return False

    # --------------------
    # SELL - strict: skip if dust
    # --------------------
    def try_sell_strict(self, current_price, reason):
        # check BTC balance
        try:
            _, btc = self.get_balances()
        except Exception as e:
            print(f"⚠️ Balance fetch failed before sell: {e}")
            return False

        btc_dec = Decimal(str(btc))
        # use primary then fallback
        symbols_to_try = [PRIMARY_SYMBOL, FALLBACK_SYMBOL]

        for sym in symbols_to_try:
            min_qty, step = self.get_lot_size(sym)
            min_notional = self.get_min_notional(sym)

            # If balance < min_qty => dust => strict skip for this symbol
            if btc_dec < min_qty:
                print(f"🟡 SELL SKIPPED on {sym} — btc {btc_dec} < minQty {min_qty} (strict).")
                continue

            # ensure notional >= minNotional
            notional = btc_dec * Decimal(str(current_price))
            if notional < min_notional:
                print(f"🟡 SELL SKIPPED on {sym} — notional {notional} < minNotional {min_notional} (strict).")
                continue

            # adjust qty down to step (safe)
            # floor(qty / step) * step
            times = (btc_dec // step)
            sell_qty = times * step
            if sell_qty < min_qty:
                print(f"🟡 After adjustment sell_qty {sell_qty} < minQty for {sym} — skipping.")
                continue

            # place market sell for sell_qty
            try:
                self.sync_server_time()
                q_str = format(sell_qty.normalize(), 'f')
                order = api_retry(self.client.order_market_sell, symbol=sym, quantity=q_str)
                # compute executed qty
                exec_qty = 0.0
                if order.get("executedQty"):
                    exec_qty = float(order["executedQty"])
                elif order.get("fills"):
                    exec_qty = sum(float(f["qty"]) for f in order["fills"])
                trade_pnl = (float(current_price) - float(self.entry_price)) * exec_qty if self.entry_price else 0.0

                # update state
                self.position = 0
                self.trades += 1
                self.realized_pnl += trade_pnl
                print(f"💰 SELL ({reason}) on {sym} @ {current_price:.2f} | Qty={exec_qty:.6f} | PnL={trade_pnl:.2f}")
                self.log_trade(reason, sym, current_price, trade_pnl, self.realized_pnl)
                self.save_state()
                return True
            except BinanceAPIException as e:
                print(f"⚠️ Sell BinanceAPIException on {sym}: {e}")
                # if insufficient balance or precision error try next symbol
                continue
            except Exception as e:
                print(f"⚠️ Sell failed on {sym}: {e}")
                continue

        # if we reach here, both symbols skipped/failed => strict skip
        print("🟡 SELL skipped on all symbols (strict).")
        return False

    # --------------------
    # Logging
    # --------------------
    def log_trade(self, action, symbol, price, trade_pnl=0.0, net_pnl=0.0):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([now, action, symbol, price, trade_pnl, net_pnl])

    # --------------------
    # Run loop
    # --------------------
    def run(self):
        print("🚀 Starting Strict-Ignore Bollinger bot")
        while True:
            try:
                self.sync_server_time()
                price = self.fetch_price(self.symbol)
                self.process_new_price(price)
                time.sleep(INTERVAL_SECS)
            except KeyboardInterrupt:
                print("🛑 Stopped by user")
                self.save_state()
                break
            except Exception as e:
                with open(ERROR_LOG, "a") as f:
                    f.write(f"{datetime.now(timezone.utc)} - {e}\n")
                print(f"⚠️ Runtime error: {e}")
                time.sleep(5)

# --------------------
# Entry
# --------------------
if __name__ == "__main__":
    bot = BollingerStrictIgnore(API_KEY, API_SECRET, PRIMARY_SYMBOL)
    bot.run()
