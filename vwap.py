import os
import time
import math
import logging
import pandas as pd
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

# =========================
# CONFIG
# =========================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
LEVERAGE = 2

EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

RISK_PER_TRADE = 0.01  # 1%

SLEEP = 60
MAX_RETRIES = 5

logging.basicConfig(level=logging.INFO)

def log(x):
    print(x)

client = Client(API_KEY, API_SECRET)

# =========================
# SAFE CALL
# =========================
def safe_call(func):
    for i in range(MAX_RETRIES):
        try:
            return func()
        except Exception as e:
            log(f"Retry {i+1}: {e}")
            time.sleep(2 ** i)
    return None

# =========================
# EXCHANGE CONFIG
# =========================
def get_filters():
    info = safe_call(lambda: client.futures_exchange_info())

    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            f = {x["filterType"]: x for x in s["filters"]}

            return {
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "min_notional": float(f["MIN_NOTIONAL"]["notional"])
            }

    return {"step": 0.001, "min_qty": 0.001, "tick": 0.1, "min_notional": 20}

F = get_filters()

# =========================
# PRECISION SAFE ROUNDING
# =========================
def round_qty(q):
    q = math.floor(q / F["step"]) * F["step"]
    return float(Decimal(str(q)).quantize(Decimal(str(F["step"])), ROUND_DOWN))

def round_price(p):
    return round(p / F["tick"]) * F["tick"]

# =========================
# POSITION SYNC (NO GHOST TRADES)
# =========================
def get_position():
    data = safe_call(lambda: client.futures_position_information(symbol=SYMBOL))
    for p in data:
        amt = float(p["positionAmt"])
        if amt != 0:
            return {
                "side": "LONG" if amt > 0 else "SHORT",
                "qty": abs(amt),
                "entry": float(p["entryPrice"])
            }
    return None

# =========================
# RISK-BASED QTY (IMPORTANT UPGRADE)
# =========================
def calc_qty(balance, price, atr):
    risk_amount = balance * RISK_PER_TRADE
    qty = risk_amount / atr  # ATR-based risk model

    qty = round_qty(qty)

    if qty * price < F["min_notional"]:
        qty = math.ceil((F["min_notional"] / price) / F["step"]) * F["step"]

    qty = round_qty(qty)

    if qty * price < F["min_notional"]:
        return None

    return qty

# =========================
# STRATEGY (YOUR EXISTING LOGIC)
# =========================
def get_signal(df):
    df["ema"] = df["close"].ewm(span=EMA_TREND).mean()
    df["rsi"] = rsi(df["close"], RSI_PERIOD)

    last = df.iloc[-1]

    if last["close"] > last["ema"] and last["rsi"] < 45:
        return "LONG"

    if last["close"] < last["ema"] and last["rsi"] > 55:
        return "SHORT"

    return None

def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(period).mean() / loss.rolling(period).mean()
    return 100 - (100 / (1 + rs))

# =========================
# EXECUTION ENGINE (FIXED CORE)
# =========================
def place_order(side, qty):
    return safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side="BUY" if side == "LONG" else "SELL",
        type="MARKET",
        quantity=qty
    ))

# =========================
# SL / TP (SAFE MODE - NO -4120)
# =========================
def place_sl_tp(side, qty, sl, tp):
    exit_side = "SELL" if side == "LONG" else "BUY"

    safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side=exit_side,
        type="STOP_MARKET",
        stopPrice=round_price(sl),
        quantity=qty,
        reduceOnly=True
    ))

    safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side=exit_side,
        type="TAKE_PROFIT_MARKET",
        stopPrice=round_price(tp),
        quantity=qty,
        reduceOnly=True
    ))

# =========================
# MAIN LOOP (PRODUCTION FLOW)
# =========================
def run():

    client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

    log("BOT STARTED (PRO MODE)")

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

        price = df["c"].iloc[-1]

        position = get_position()

        signal = get_signal(df)

        if position:
            log(f"Position exists: {position}")
            time.sleep(SLEEP)
            continue

        if not signal:
            time.sleep(SLEEP)
            continue

        balance = float(client.futures_account_balance()[0]["balance"])

        atr_val = (df["h"] - df["l"]).rolling(14).mean().iloc[-1]

        qty = calc_qty(balance, price, atr_val)

        if not qty:
            log("Qty invalid")
            time.sleep(SLEEP)
            continue

        log(f"Signal: {signal} Qty: {qty}")

        order = place_order(signal, qty)

        if not order:
            continue

        avg = price

        if signal == "LONG":
            sl = avg - atr_val
            tp = avg + atr_val * 1.5
        else:
            sl = avg + atr_val
            tp = avg - atr_val * 1.5

        place_sl_tp(signal, qty, sl, tp)

        log(f"{signal} executed @ {avg}")

        time.sleep(SLEEP)

# =========================
run()