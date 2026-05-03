import os
import time
import math
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager
from collections import deque

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
# GLOBAL STATE
# =============================
current_trade = {"side": None, "entry": None, "sl_id": None, "tp_id": None}

# Incremental indicator state
prev_close = None
ema = None
alpha = 2 / (EMA_TREND + 1)

atr = None
rsi_avg_gain = None
rsi_avg_loss = None

vwap_queue = deque(maxlen=VWAP_WINDOW)
vwap_pv_sum = 0
vwap_vol_sum = 0

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
# LOAD HISTORY + INIT
# =============================
def load_history():
    global prev_close, ema, atr, rsi_avg_gain, rsi_avg_loss
    global vwap_pv_sum, vwap_vol_sum

    klines = client.futures_klines(symbol=SYMBOL_UPPER, interval="5m", limit=300)

    closes, highs, lows, volumes = [], [], [], []

    for k in klines:
        closes.append(float(k[4]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))
        volumes.append(float(k[5]))

    # EMA init
    ema_series = closes[0]
    for c in closes:
        ema_series = (c - ema_series) * alpha + ema_series
    ema = ema_series

    # ATR init
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    atr = sum(tr_list[-ATR_PERIOD:]) / ATR_PERIOD

    # RSI init
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    rsi_avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD

    # VWAP init
    for i in range(len(closes)):
        pv = closes[i] * volumes[i]
        vwap_queue.append((pv, volumes[i]))
        vwap_pv_sum += pv
        vwap_vol_sum += volumes[i]

    prev_close = closes[-1]

# =============================
# UPDATE INDICATORS
# =============================
def update_indicators(candle):
    global ema, atr, rsi_avg_gain, rsi_avg_loss, prev_close
    global vwap_pv_sum, vwap_vol_sum

    high, low, close, volume = candle["high"], candle["low"], candle["close"], candle["volume"]

    # EMA
    ema = (close - ema) * alpha + ema

    # ATR
    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
    atr = ((atr * (ATR_PERIOD - 1)) + tr) / ATR_PERIOD

    # RSI
    change = close - prev_close
    gain = max(change, 0)
    loss = max(-change, 0)

    rsi_avg_gain = ((rsi_avg_gain * (RSI_PERIOD - 1)) + gain) / RSI_PERIOD
    rsi_avg_loss = ((rsi_avg_loss * (RSI_PERIOD - 1)) + loss) / RSI_PERIOD

    rs = rsi_avg_gain / rsi_avg_loss if rsi_avg_loss != 0 else 0
    rsi = 100 - (100 / (1 + rs))

    # VWAP
    pv = close * volume
    if len(vwap_queue) == VWAP_WINDOW:
        old_pv, old_vol = vwap_queue.popleft()
        vwap_pv_sum -= old_pv
        vwap_vol_sum -= old_vol

    vwap_queue.append((pv, volume))
    vwap_pv_sum += pv
    vwap_vol_sum += volume
    vwap_val = vwap_pv_sum / vwap_vol_sum if vwap_vol_sum != 0 else close

    prev_close = close

    return {"EMA": ema, "ATR": atr, "RSI": rsi, "VWAP": vwap_val, "close": close}

# =============================
# EXECUTE TRADE
# =============================
def execute_trade(ind):
    global current_trade

    if current_trade["side"] is not None:
        return

    price, atr_val = ind["close"], ind["ATR"]

    if has_position():
        return

    if price > ind["EMA"] and price < ind["VWAP"] * 1.002 and ind["RSI"] < 45:
        side = "LONG"
    elif price < ind["EMA"] and price > ind["VWAP"] * 0.998 and ind["RSI"] > 55:
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

    current_trade = {"side": side, "entry": entry, "sl_id": sl_order["orderId"], "tp_id": tp_order["orderId"]}

    print(f"✅ {side} ENTRY @ {entry} | SL {sl} | TP {tp}", flush=True)

# =============================
# SOCKET
# =============================
def handle_socket(msg):
    if msg['e'] != 'kline':
        return

    k = msg['k']
    if not k['x']:
        return

    candle = {
        "open": float(k['o']),
        "high": float(k['h']),
        "low": float(k['l']),
        "close": float(k['c']),
        "volume": float(k['v'])
    }

    ind = update_indicators(candle)

    print(f"📡 Close: {ind['close']} | RSI: {ind['RSI']:.2f}", flush=True)

    execute_trade(ind)

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
        time.sleep(2)