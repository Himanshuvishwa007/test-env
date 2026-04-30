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
# CONFIG
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
LEVERAGE = 2
SLEEP = 60
MAX_RETRIES = 5

# =============================
# LOGGING
# =============================
logging.basicConfig(level=logging.INFO)

def log(x):
    print(x)

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
# DETECT MODE (CRITICAL)
# =============================
def is_hedge_mode():
    acc = safe_call(lambda: client.futures_account())
    return acc.get("dualSidePosition", False)

HEDGE_MODE = is_hedge_mode()
log(f"Hedge Mode: {HEDGE_MODE}")

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

# =============================
# PRECISION
# =============================
def round_qty(q):
    q = math.floor(q / F["step"]) * F["step"]
    return float(Decimal(str(q)).quantize(Decimal(str(F["step"])), ROUND_DOWN))

# =============================
# POSITION CHECK
# =============================
def get_position():
    data = safe_call(lambda: client.futures_position_information(symbol=SYMBOL))
    for p in data:
        amt = float(p["positionAmt"])
        if amt != 0:
            return abs(amt)
    return 0

# =============================
# EMERGENCY CLOSE
# =============================
def emergency_close(qty, side):
    log("🚨 Emergency closing position")

    safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side="SELL" if side == "LONG" else "BUY",
        type="MARKET",
        quantity=qty,
        reduceOnly=True
    ))

# =============================
# SL/TP (MODE SAFE)
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

    # Hedge mode requires positionSide
    if HEDGE_MODE:
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

    if not sl_order or not tp_order:
        return False

    return True

# =============================
# TRADE
# =============================
def open_trade():

    if get_position() != 0:
        log("Position exists")
        return

    balance = float(client.futures_account_balance()[0]["balance"])
    price = float(client.futures_mark_price(symbol=SYMBOL)["markPrice"])

    qty = round_qty((balance * LEVERAGE) / price)

    if qty * price < F["min_notional"]:
        log("Qty too small")
        return

    side = "LONG"

    order = safe_call(lambda: client.futures_create_order(
        symbol=SYMBOL,
        side="BUY",
        type="MARKET",
        quantity=qty
    ))

    if not order:
        return

    avg_price = float(order.get("avgPrice", 0))
    if avg_price == 0:
        avg_price = price

    log(f"Entry @ {avg_price}")

    sl = avg_price * 0.995
    tp = avg_price * 1.01

    success = place_sl_tp(side, qty, sl, tp)

    if not success:
        log("❌ SL/TP failed")
        emergency_close(qty, side)
        return

    log("✅ Trade protected")

# =============================
# LOOP
# =============================
def run():
    log("BOT STARTED")

    while True:
        try:
            open_trade()
        except Exception as e:
            log(f"Error: {e}")

        time.sleep(SLEEP)

# =============================
run()