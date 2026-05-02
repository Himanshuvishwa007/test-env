import os
import time
import math
import pandas as pd
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager

# =============================
# CONFIG
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ethusdt"
SYMBOL_UPPER = "ETHUSDT"

LEVERAGE = 2
RISK_PERCENT = 0.3

EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_WINDOW = 288

client = Client(API_KEY, API_SECRET)

# =============================
# FILTERS
# =============================
def get_filters():
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL_UPPER:
            f = {x["filterType"]: x for x in s["filters"]}
            return {
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "min_notional": float(f["MIN_NOTIONAL"]["notional"])
            }

F = get_filters()

def round_qty(q):
    q = math.floor(q / F["step"]) * F["step"]
    return float(Decimal(str(q)).quantize(Decimal(str(F["step"])), ROUND_DOWN))

# =============================
# BALANCE
# =============================
def get_balance():
    balances = client.futures_account_balance()
    for b in balances:
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0

# =============================
# INDICATORS
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
    pos = client.futures_position_information(symbol=SYMBOL_UPPER)
    for p in pos:
        if float(p["positionAmt"]) != 0:
            return True
    return False

# =============================
# ORDER EXECUTION
# =============================
def execute_trade(df):

    if len(df) < 250:
        print("Waiting for enough data...", flush=True)
        return

    last = df.iloc[-1]
    price = last["close"]

    if has_position():
        print("Position exists", flush=True)
        return

    if price > last["EMA"] and price < last["VWAP"] * 1.002 and last["RSI"] < 45:
        side = "LONG"
    elif price < last["EMA"] and price > last["VWAP"] * 0.998 and last["RSI"] > 55:
        side = "SHORT"
    else:
        print("No signal", flush=True)
        return

    balance = get_balance()
    qty = round_qty((balance * RISK_PERCENT * LEVERAGE) / price)
    notional = qty * price

    print(f"Balance: {balance}, Qty: {qty}", flush=True)

    if qty < F["min_qty"] or notional < F["min_notional"]:
        print("❌ Qty too small", flush=True)
        return

    order = client.futures_create_order(
        symbol=SYMBOL_UPPER,
        side="BUY" if side=="LONG" else "SELL",
        type="MARKET",
        quantity=qty
    )

    print(f"✅ {side} executed", flush=True)

# =============================
# DATA
# =============================
df = pd.DataFrame(columns=["open","high","low","close","volume"])

def load_history():
    global df
    print("Loading historical...", flush=True)

    klines = client.futures_klines(
        symbol=SYMBOL_UPPER,
        interval="5m",
        limit=300
    )

    data = []
    for k in klines:
        data.append({
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5])
        })

    df = pd.DataFrame(data)

    df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
    df["RSI"] = rsi(df["close"], RSI_PERIOD)
    df["ATR"] = atr(df, ATR_PERIOD)
    df["VWAP"] = vwap(df)

    print("✅ History loaded", flush=True)

# =============================
# SOCKET CALLBACK (FIXED)
# =============================
def handle_socket(msg):
    global df

    if msg['e'] != 'kline':
        return

    k = msg['k']

    print(f"📡 Tick: {k['c']} | Closed: {k['x']}", flush=True)

    if not k['x']:
        return

    new_row = {
        "open": float(k['o']),
        "high": float(k['h']),
        "low": float(k['l']),
        "close": float(k['c']),
        "volume": float(k['v'])
    }

    df = pd.concat([df, pd.DataFrame([new_row])]).tail(300)

    df["EMA"] = df["close"].ewm(span=EMA_TREND).mean()
    df["RSI"] = rsi(df["close"], RSI_PERIOD)
    df["ATR"] = atr(df, ATR_PERIOD)
    df["VWAP"] = vwap(df)

    print("📊 Candle closed", flush=True)

    execute_trade(df)

# =============================
# START SOCKET (FINAL FIX)
# =============================
def start_socket():
    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()

    print("✅ Socket Started", flush=True)

    twm.start_kline_socket(
        callback=handle_socket,
        symbol=SYMBOL_UPPER,
        interval="5m"
    )

    while True:
        time.sleep(1)

# =============================
# MAIN
# =============================
if __name__ == "__main__":
    print("🚀 Bot Started", flush=True)

    load_history()
    start_socket()