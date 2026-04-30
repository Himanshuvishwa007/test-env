import os
import time
import math
import logging
import pandas as pd
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

# =============================
# CONFIG (UNCHANGED STRATEGY PARAMS)
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
LEVERAGE = 2

EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

TP_MULT = 1.5
SL_MULT = 1.0

SLEEP = 60
MAX_RETRIES = 5

# =============================
logging.basicConfig(level=logging.INFO)
def log(x): print(x)

client = Client(API_KEY, API_SECRET)

# =============================
# SAFE CALL
# =============================
def safe_call(func):
    for i in range(MAX_RETRIES):
        try:
            return func()
        except Exception as e:
            log(f"Retry {i+1}: {e}")
            time.sleep(2 ** i)
    return None

# =============================
# MODE DETECTION
# =============================
def is_hedge():
    acc = safe_call(lambda: client.futures_account())
    return acc.get("dualSidePosition", False)

HEDGE = is_hedge()
log(f"Hedge Mode: {HEDGE}")

# =============================
# FILTERS
# =============================
def get_filters():
    info = safe_call(lambda: client.futures_exchange_info())
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            f = {x["filterType"]: x for x in s["filters"]}
            return {
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_notional": float(f["MIN_NOTIONAL"]["notional"])
            }
    return {"step": 0.001, "min_notional": 20}

F = get_filters()

def round_qty(q):
    q = math.floor(q / F["step"]) * F["step"]
    return float(Decimal(str(q)).quantize(Decimal(str(F["step"])), ROUND_DOWN))

# =============================
# INDICATORS (YOUR ORIGINAL)
# =============================
def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(period).mean() / loss.rolling(period).mean()
    return 100 - (100/(1+rs))

def atr(df, period):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def vwap(df):
    pv = (df["close"] * df["volume"]).rolling(VWAP_WINDOW).sum()
    vol = df["volume"].rolling(VWAP_WINDOW).sum()
    return pv / vol

# =============================
# POSITION CHECK
# =============================
def has_position():
    pos = safe_call(lambda: client.futures_position_information(symbol=SYMBOL))
    for p in pos:
        if float(p["positionAmt"]) != 0:
            return True
    return False

# =============================
# EMERGENCY CLOSE
# =============================
def emergency_close(qty, side):
    log("🚨 Emergency closing position")
    safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side="SELL" if side=="LONG" else "BUY",
        type="MARKET",
        quantity=qty,
        reduceOnly=True
    ))

# =============================
# SL/TP (FIXED)
# =============================
def place_sl_tp(side, qty, sl, tp):

    exit_side = "SELL" if side == "LONG" else "BUY"

    params = {
        "symbol": SYMBOL,
        "side": exit_side,
        "quantity": qty,
        "reduceOnly": True,
        "workingType": "MARK_PRICE"
    }

    if HEDGE:
        params["positionSide"] = side

    sl_order = safe_call(lambda: client.futures_create_order(
        **params,
        type="STOP_MARKET",
        stopPrice=sl
    ))

    tp_order = safe_call(lambda: client.futures_create_order(
        **params,
        type="TAKE_PROFIT_MARKET",
        stopPrice=tp
    ))

    return sl_order and tp_order

# =============================
# MAIN LOGIC (YOUR STRATEGY RESTORED)
# =============================
def run():

    log("BOT STARTED (VWAP RESTORED)")

    while True:

        df = safe_call(lambda: client.futures_klines(
            symbol=SYMBOL,
            interval="5m",
            limit=300
        ))

        if not df:
            continue

        df = pd.DataFrame(df, columns=[
            "t","o","h","l","c","v",
            "_","_","_","_","_","_"
        ])

        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)

        # ===== INDICATORS =====
        df["EMA"] = df["c"].ewm(span=EMA_TREND).mean()
        df["RSI"] = rsi(df["c"], RSI_PERIOD)
        df["ATR"] = atr(df, ATR_PERIOD)
        df["VWAP"] = vwap(df)

        df = df.dropna()
        last = df.iloc[-1]

        price = last["c"]

        if has_position():
            log("Position exists")
            time.sleep(SLEEP)
            continue

        # ===== YOUR EXACT ENTRY LOGIC =====
        if price > last["EMA"] and price < last["VWAP"] * 1.002 and last["RSI"] < 45:
            side = "LONG"

        elif price < last["EMA"] and price > last["VWAP"] * 0.998 and last["RSI"] > 55:
            side = "SHORT"

        else:
            time.sleep(SLEEP)
            continue

        # ===== QTY =====
        balance = float(client.futures_account_balance()[0]["balance"])
        qty = round_qty((balance * LEVERAGE) / price)

        if qty * price < F["min_notional"]:
            log("Qty too small")
            time.sleep(SLEEP)
            continue

        # ===== ENTRY =====
        order = safe_call(lambda: client.futures_create_order(
            symbol=SYMBOL,
            side="BUY" if side=="LONG" else "SELL",
            type="MARKET",
            quantity=qty
        ))

        if not order:
            continue

        avg = float(order.get("avgPrice", 0)) or price
        log(f"{side} @ {avg}")

        # ===== ATR SL/TP (UNCHANGED IDEA) =====
        if side == "LONG":
            sl = avg - last["ATR"] * SL_MULT
            tp = avg + last["ATR"] * TP_MULT
        else:
            sl = avg + last["ATR"] * SL_MULT
            tp = avg - last["ATR"] * TP_MULT

        # ===== PROTECTION =====
        if not place_sl_tp(side, qty, sl, tp):
            log("❌ SL/TP failed")
            emergency_close(qty, side)

        time.sleep(SLEEP)

# =============================
run()