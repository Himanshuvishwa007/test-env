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

EMA_TREND = 198
RSI_PERIOD = 15
ATR_PERIOD = 15
VWAP_WINDOW = 51

TP_MULT = 1.5
SL_MULT = 1.0

TRADE_LOG_FILE = "trades.csv"

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
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"])
            }

F = get_filters()

def round_qty(q):
    q = math.floor(q / F["step"]) * F["step"]
    return float(Decimal(str(q)).quantize(Decimal(str(F["step"])), ROUND_DOWN))

def round_price(p):
    step = F["tick"]
    return float(Decimal(str(p)).quantize(Decimal(str(step)), ROUND_DOWN))

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
# GLOBAL STATE
# =============================
df = pd.DataFrame()
current_trade = {
    "side": None,
    "entry": None,
    "sl_id": None,
    "tp_id": None
}

# =============================
# BALANCE
# =============================
def get_balance():
    balances = client.futures_account_balance()
    for b in balances:
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0

def has_position():
    pos = client.futures_position_information(symbol=SYMBOL_UPPER)
    for p in pos:
        if float(p["positionAmt"]) != 0:
            return True
    return False

# =============================
# LOGGING
# =============================
def log_trade(entry, exit_price, side):
    pnl = (exit_price - entry) if side == "LONG" else (entry - exit_price)
    with open(TRADE_LOG_FILE, "a") as f:
        f.write(f"{time.time()},{side},{entry},{exit_price},{pnl}\n")
    print(f"📊 PnL: {pnl}", flush=True)

# =============================
# EXECUTE TRADE
# =============================
def execute_trade(df):
    global current_trade

    if current_trade["side"] is not None:
        return

    last = df.iloc[-1]
    price = last["close"]
    atr_val = last["ATR"]

    if has_position():
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

    if qty < F["min_qty"]:
        print("Qty too small", flush=True)
        return

    entry_order = client.futures_create_order(
        symbol=SYMBOL_UPPER,
        side="BUY" if side == "LONG" else "SELL",
        type="MARKET",
        quantity=qty
    )

    entry = float(entry_order.get("avgPrice", 0)) or price

    if side == "LONG":
        sl = round_price(entry - atr_val * SL_MULT)
        tp = round_price(entry + atr_val * TP_MULT)
        exit_side = "SELL"
    else:
        sl = round_price(entry + atr_val * SL_MULT)
        tp = round_price(entry - atr_val * TP_MULT)
        exit_side = "BUY"

    sl_order = client.futures_create_order(
        symbol=SYMBOL_UPPER,
        side=exit_side,
        type="STOP_MARKET",
        stopPrice=sl,
        closePosition=True
    )

    tp_order = client.futures_create_order(
        symbol=SYMBOL_UPPER,
        side=exit_side,
        type="TAKE_PROFIT_MARKET",
        stopPrice=tp,
        closePosition=True
    )

    current_trade = {
        "side": side,
        "entry": entry,
        "sl_id": sl_order["orderId"],
        "tp_id": tp_order["orderId"]
    }

    print(f"✅ {side} ENTRY @ {entry} | SL {sl} | TP {tp}", flush=True)

# =============================
# MONITOR ORDERS
# =============================
def monitor_orders():
    global current_trade

    if current_trade["side"] is None:
        return

    orders = client.futures_get_open_orders(symbol=SYMBOL_UPPER)
    open_ids = [o["orderId"] for o in orders]

    sl_alive = current_trade["sl_id"] in open_ids
    tp_alive = current_trade["tp_id"] in open_ids

    if not sl_alive or not tp_alive:
        print("🎯 Trade closed", flush=True)

        for o in orders:
            if o["orderId"] in [current_trade["sl_id"], current_trade["tp_id"]]:
                client.futures_cancel_order(symbol=SYMBOL_UPPER, orderId=o["orderId"])

        mark = client.futures_mark_price(symbol=SYMBOL_UPPER)
        exit_price = float(mark["markPrice"])

        log_trade(current_trade["entry"], exit_price, current_trade["side"])

        current_trade = {
            "side": None,
            "entry": None,
            "sl_id": None,
            "tp_id": None
        }

# =============================
# LOAD HISTORY
# =============================
def load_history():
    global df

    klines = client.futures_klines(symbol=SYMBOL_UPPER, interval="5m", limit=300)

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

# =============================
# SOCKET
# =============================
def handle_socket(msg):
    global df

    if msg['e'] != 'kline':
        return

    k = msg['k']

    print(f"📡 {k['c']} | Closed: {k['x']}", flush=True)

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

    execute_trade(df)

# =============================
# MAIN
# =============================
if __name__ == "__main__":
    print("🚀 Bot Started", flush=True)

    load_history()

    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()

    twm.start_kline_socket(
        callback=handle_socket,
        symbol=SYMBOL_UPPER,
        interval="5m"
    )

    while True:
        monitor_orders()
        time.sleep(2)