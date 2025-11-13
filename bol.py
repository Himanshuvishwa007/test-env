#!/usr/bin/env python3
"""
bol.py - Final production-ready Bollinger Spot bot with:
 - LOT_SIZE and MIN_NOTIONAL handling
 - Insufficient-balance safe sells
 - Time-sync and retry/backoff for API calls
 - FDUSD primary with BTCUSDT fallback for sells
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
# 🔑 API KEYS: prefer env vars
# -----------------------------
api_key = "cL4LHJfRXt9FO2p3f65GGvqBmypnlH2jihupQU8uHJEv1BSrC48P6YWSQJdUVgV6"
api_secret = "lkhKrYkoMIldhtabkdU1KyiUi2WyCj7gD8GMYDILZ0vZg7iIZWEGuEE63JqjtpQG"

# -----------------------------
# ⚙️ TRADING CONFIG
# -----------------------------
PRIMARY_SYMBOL = "BTCFDUSD"   # primary symbol (keep)
FALLBACK_SYMBOL = "BTCUSDT"   # fallback if primary cant sell
SYMBOL = PRIMARY_SYMBOL       # used by strategy (we can keep primary)
INTERVAL_SECS = 60
BB_WINDOW = 20
BB_STD_DEV = 3
SMA_FILTER_WINDOW = 30
TP_PCT = 0.01         
SL_PCT = 0.009          
CASH_RESERVE_PCT = 0.001
BASE_RESERVE_PCT = 0.001

STATE_FILE = "trade_state.json"
TRADE_LOG = "trade_log_live.csv"
ERROR_LOG = "error_log.txt"

# Safety / retries
API_MAX_RETRIES = 5
API_BACKOFF_BASE = 0.5  # seconds

# -----------------------------
# 🧠 Helper: retry wrapper
# -----------------------------
def api_retry(fn, *args, **kwargs):
    """Call fn with retries and exponential backoff on common network errors."""
    backoff = API_BACKOFF_BASE
    for attempt in range(API_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            # For Binance errors, raise to caller to handle specific codes
            raise
        except Exception as e:
            # network or other transient errors -> retry
            if attempt < API_MAX_RETRIES - 1:
                print(f"⚠️ Transient error: {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                raise

# -----------------------------
# 🧠 Bot Class
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

        # caches for symbol filters
        self._symbol_filters = {}

        self.load_state()
        self.sync_server_time()
        self.detect_existing_position()

        if not os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Action", "Symbol", "Price", "TradePnL", "NetPnL"])

        print(f"✅ Live Bot initialized for {symbol}")
        print(f"⚙️ Params: BB Window={BB_WINDOW}, BB Std={BB_STD_DEV}, SMA Filter={SMA_FILTER_WINDOW}, TP={TP_PCT*100:.2f}%, SL={SL_PCT*100:.2f}%")

    # -----------------------------
    # 🔹 Time sync
    # -----------------------------
    def sync_server_time(self):
        try:
            srv = api_retry(self.client.get_server_time)
            # we don't set local clock; just ensure connectivity and server time accessible
            if srv and 'serverTime' in srv:
                print(f"🔁 Binance server time OK.")
        except Exception as e:
            print(f"⚠️ Warning: couldn't fetch server time: {e}")

    # -----------------------------
    # 🔹 Symbol filter helpers
    # -----------------------------
    def _get_symbol_info(self, symbol):
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]
        info = api_retry(self.client.get_symbol_info, symbol)
        self._symbol_filters[symbol] = info
        return info

    def get_filters(self, symbol):
        """Return dict of filters for symbol (LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER)"""
        info = self._get_symbol_info(symbol)
        filters = {}
        for f in info.get("filters", []):
            filters[f["filterType"]] = f
        return filters

    def get_lot_size_decimals(self, symbol):
        filters = self.get_filters(symbol)
        lot = filters.get("LOT_SIZE", {})
        minQty = Decimal(str(lot.get("minQty", "0.00000001")))
        stepSize = Decimal(str(lot.get("stepSize", "0.00000001")))
        # compute decimal places from stepSize
        return minQty, stepSize

    def get_min_notional(self, symbol):
        filters = self.get_filters(symbol)
        notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or filters.get("MARKET_LOT_SIZE")
        if notional and "minNotional" in notional:
            return Decimal(str(notional["minNotional"]))
        # fallback to small value
        return Decimal("1")

    def adjust_qty_down_decimal(self, qty_decimal, step_decimal, min_qty_decimal):
        """Round down qty_decimal to nearest multiple of step_decimal, return Decimal or 0."""
        if qty_decimal <= 0:
            return Decimal("0")
        # floor(qty / step) * step
        times = (qty_decimal // step_decimal)
        adjusted = times * step_decimal
        if adjusted < min_qty_decimal:
            return Decimal("0")
        return adjusted

    # -----------------------------
    # 🔹 State persistence
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
    # 🔹 Balances & entry detection
    # -----------------------------
    def get_balances(self):
        resp = api_retry(self.client.get_account)
        balances = resp.get("balances", [])
        df = pd.DataFrame(balances)
        df.set_index("asset", inplace=True)
        fdusd = float(df.loc["FDUSD", "free"]) if "FDUSD" in df.index else 0.0
        btc = float(df.loc["BTC", "free"]) if "BTC" in df.index else 0.0
        return fdusd, btc

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
            print(f"⚠️ Failed to infer entry price: {e}")
            return None

    # -----------------------------
    # 🔹 Order helpers: min notional check
    # -----------------------------
    def check_min_notional(self, symbol, price, qty_decimal):
        min_notional = self.get_min_notional(symbol)
        notional = (Decimal(str(price)) * qty_decimal)
        if notional < min_notional:
            return False, min_notional, notional
        return True, min_notional, notional

    # -----------------------------
    # 🔹 Buy (market) - uses quoteOrderQty so Binance sets qty
    # -----------------------------
    def buy_all(self, symbol=None):
        sym = symbol or self.symbol
        try:
            fdusd, _ = self.get_balances()
            spend_amt = fdusd * (1 - CASH_RESERVE_PCT)
            if spend_amt < 5:
                print(f"⚠️ Not enough FDUSD to buy ({spend_amt:.2f}) on {sym}")
                return False

            # ensure time sync and retry wrapper
            self.sync_server_time()
            order = api_retry(self.client.order_market_buy, symbol=sym, quoteOrderQty=round(spend_amt, 2))
            fills = order.get("fills", [])
            qty = sum(float(f["qty"]) for f in fills)
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            self.entry_price = total_cost / qty if qty > 0 else self.fetch_price(sym)
            self.position = 1
            self.trades += 1
            print(f"📈 BUY ({sym}) @ {self.entry_price:.2f} | Qty={qty:.6f} | Spent≈{spend_amt:.2f}")
            self.log_trade("BUY", sym, self.entry_price)
            self.save_state()
            return True
        except BinanceAPIException as e:
            print(f"⚠️ Buy failed on {sym}: {e}")
            return False
        except Exception as e:
            print(f"⚠️ Buy unexpected error on {sym}: {e}")
            return False

    # -----------------------------
    # 🔹 Sell (market) - robust with retries and fallback
    # -----------------------------
    def sell_all(self, price, reason):
        # try primary then fallback
        for attempt_symbol in [PRIMARY_SYMBOL, FALLBACK_SYMBOL]:
            sold = self._sell_with_retries(attempt_symbol, price, reason)
            if sold:
                return True
        print("⚠️ Sell failed on both primary and fallback symbols.")
        return False

    def _sell_with_retries(self, symbol, price, reason):
        try:
            # fetch base asset balance (BTC)
            bal = api_retry(self.client.get_asset_balance, asset="BTC")
            balance = float(bal.get("free", 0.0)) if bal else 0.0
            if balance <= 0:
                print(f"⚠️ No BTC balance to sell on {symbol}")
                return False

            # get filters
            min_qty, step = self.get_lot_size_decimals(symbol)
            # decimal conversion
            balance_dec = Decimal(str(balance))
            step_dec = step
            min_qty_dec = min_qty

            # apply base reserve
            qty_dec = balance_dec * (Decimal("1") - Decimal(str(BASE_RESERVE_PCT)))
            qty_dec = self.adjust_qty_safe(qty_dec, step_dec, min_qty_dec)

            if qty_dec == Decimal("0"):
                print(f"⚠️ Qty below min for {symbol} after adjustment; balance={balance}")
                return False

            # check min notional (must be >= allowed)
            ok, min_notional, notional = self.check_min_notional(symbol, price, qty_dec)
            if not ok:
                print(f"⚠️ Notional {notional} < minNotional {min_notional} for {symbol}. Skipping sell.")
                return False

            # attempt progressive retries decreasing by step if precision/lot issues
            max_attempts = 6
            for attempt in range(max_attempts):
                try_qty = qty_dec - (step_dec * Decimal(attempt))
                if try_qty < min_qty_dec:
                    print(f"⚠️ try_qty < minQty on {symbol}, cancel attempt.")
                    break
                q_str = format(try_qty.normalize(), 'f')  # string w/o exponent
                # sync time
                self.sync_server_time()
                order = api_retry(self.client.order_market_sell, symbol=symbol, quantity=q_str)
                # extract executed qty
                executed_qty = 0.0
                if order.get("executedQty"):
                    executed_qty = float(order["executedQty"])
                elif order.get("fills"):
                    executed_qty = sum(float(f["qty"]) for f in order["fills"])
                trade_pnl = (price - float(self.entry_price)) * executed_qty if self.entry_price else 0.0

                # update state
                self.position = 0
                self.trades += 1
                self.realized_pnl += trade_pnl
                print(f"💰 SELL ({reason}) on {symbol} @ {price:.2f} | Qty={executed_qty:.6f} | PnL={trade_pnl:.2f} | NetPnL={self.realized_pnl:.2f}")
                self.log_trade(reason, symbol, price, trade_pnl, self.realized_pnl)
                self.save_state()
                return True

        except BinanceAPIException as e:
            # handle -2010 insufficient balance specially (attempt to refetch and try once)
            err_msg = str(e)
            code = None
            try:
                code = e.code
            except Exception:
                pass
            print(f"⚠️ BinanceAPIException during sell on {symbol}: {err_msg}")
            if code == -2010 or "insufficient" in err_msg.lower():
                # resync and one final attempt
                try:
                    time.sleep(0.5)
                    bal = api_retry(self.client.get_asset_balance, asset="BTC")
                    balance = float(bal.get("free", 0.0)) if bal else 0.0
                    if balance <= 0:
                        return False
                    min_qty, step = self.get_lot_size_decimals(symbol)
                    balance_dec = Decimal(str(balance))
                    qty_dec = balance_dec * (Decimal("1") - Decimal(str(BASE_RESERVE_PCT)))
                    qty_dec = self.adjust_qty_safe(qty_dec, step, min_qty)
                    if qty_dec == Decimal("0"):
                        return False
                    q_str = format(qty_dec.normalize(), 'f')
                    self.sync_server_time()
                    order = api_retry(self.client.order_market_sell, symbol=symbol, quantity=q_str)
                    exec_qty = float(order.get("executedQty") or 0.0)
                    trade_pnl = (price - float(self.entry_price)) * exec_qty if self.entry_price else 0.0
                    self.position = 0
                    self.trades += 1
                    self.realized_pnl += trade_pnl
                    print(f"💰 SELL (retry) on {symbol} @ {price:.2f} | Qty={exec_qty:.6f} | PnL={trade_pnl:.2f}")
                    self.log_trade(reason, symbol, price, trade_pnl, self.realized_pnl)
                    self.save_state()
                    return True
                except Exception as ex:
                    print(f"⚠️ Retry sell failed: {ex}")
                    return False
            return False
        except Exception as e:
            print(f"⚠️ Unexpected error selling on {symbol}: {e}")
            return False

    def adjust_qty_safe(self, qty_decimal, step_dec, min_qty_dec):
        """Wrapper to ensure Decimal step/min handling robustly."""
        try:
            return self.adjust_qty_down_decimal(qty_decimal, step_dec, min_qty_dec)
        except Exception:
            # fallback to quantize
            try:
                adjusted = qty_decimal.quantize(step_dec, rounding=ROUND_DOWN)
                return adjusted if adjusted >= min_qty_dec else Decimal("0")
            except Exception:
                return Decimal("0")

    # -----------------------------
    # 🔹 Strategy & main loop
    # -----------------------------
    def fetch_price(self, symbol=None):
        sym = symbol or self.symbol
        try:
            t = api_retry(self.client.get_symbol_ticker, symbol=sym)
            return float(t["price"])
        except Exception as e:
            print(f"⚠️ Price fetch error for {sym}: {e}")
            return None

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
                    print("⚠️ Sell attempt failed; resyncing balances and will retry on next loop.")
                    self.detect_existing_position()

        # concise logging
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
    # 🔹 Run loop
    # -----------------------------
    def run(self):
        print(f"\n🚀 Running LIVE BOT for {self.symbol}")
        print("Press Ctrl + C to stop.\n")
        while True:
            try:
                # refresh server time periodically
                self.sync_server_time()
                price = self.fetch_price(self.symbol)
                self.process_new_price(price)
                time.sleep(INTERVAL_SECS)
            except KeyboardInterrupt:
                print("\n🛑 Bot stopped manually.")
                self.save_state()
                break
            except Exception as e:
                # log and continue
                with open(ERROR_LOG, "a") as f:
                    f.write(f"{datetime.now(timezone.utc)} - {e}\n")
                print(f"⚠️ Runtime error: {e}")
                time.sleep(5)

# -----------------------------
# 🚀 START BOT
# -----------------------------
if __name__ == "__main__":
    bot = BollingerSpotLive(api_key, api_secret, PRIMARY_SYMBOL)
    bot.run()
