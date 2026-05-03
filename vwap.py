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

INTERVAL = "1m"   # 🔥 change back to 5m after testing

LEVERAGE = 2
RISK_PERCENT = 0.3

EMA_TREND = 198
RSI_PERIOD = 15
ATR_PERIOD = 15
VWAP_WINDOW = 51

TP_MULT = 1.5
SL_MULT = 1.0

ERROR_LOG_FILE = "error.log"

client = Client(API_KEY, API_SECRET)

# =============================
# LOGGING
# =============================
def log_error(msg):
    with open(ERROR_LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")

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
prev_close = None
ema = None
alpha = 2 / (EMA_TREND + 1)

atr = None
rsi_avg_gain = None
rsi_avg_loss = None

vwap_queue = deque(maxlen=VWAP_WINDOW)
vwap_pv_sum = 0
vwap_vol_sum = 0

last_msg_time = time.time()

# =============================
# LOAD HISTORY
# =============================
def load_history():
    global prev_close, ema, atr, rsi_avg_gain, rsi_avg_loss
    global vwap_pv_sum, vwap_vol_sum

    print("📥 Loading history...")

    klines = client.futures_klines(symbol=SYMBOL_UPPER, interval=INTERVAL, limit=300)

    closes, highs, lows, volumes = [], [], [], []

    for k in klines:
        closes.append(float(k[4]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))
        volumes.append(float(k[5]))

    ema_series = closes[0]
    for c in closes:
        ema_series = (c - ema_series) * alpha + ema_series
    ema = ema_series

    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    atr = sum(tr_list[-ATR_PERIOD:]) / ATR_PERIOD

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    rsi_avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD

    for i in range(len(closes)):
        pv = closes[i] * volumes[i]
        vwap_queue.append((pv, volumes[i]))
        vwap_pv_sum += pv
        vwap_vol_sum += volumes[i]

    prev_close = closes[-1]

    print("✅ History Loaded")

# =============================
# UPDATE INDICATORS
# =============================
def update_indicators(candle):
    global ema, atr, rsi_avg_gain, rsi_avg_loss, prev_close
    global vwap_pv_sum, vwap_vol_sum

    high, low, close, volume = candle["high"], candle["low"], candle["close"], candle["volume"]

    ema = (close - ema) * alpha + ema

    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
    atr = ((atr * (ATR_PERIOD - 1)) + tr) / ATR_PERIOD

    change = close - prev_close
    gain = max(change, 0)
    loss = max(-change, 0)

    rsi_avg_gain = ((rsi_avg_gain * (RSI_PERIOD - 1)) + gain) / RSI_PERIOD
    rsi_avg_loss = ((rsi_avg_loss * (RSI_PERIOD - 1)) + loss) / RSI_PERIOD

    rs = rsi_avg_gain / rsi_avg_loss if rsi_avg_loss != 0 else 0
    rsi = 100 - (100 / (1 + rs))

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

    return {"close": close, "RSI": rsi}

# =============================
# SOCKET HANDLER
# =============================
def handle_socket(msg):
    global last_msg_time
    last_msg_time = time.time()

    try:
        print("📩 Data received")  # heartbeat

        if msg.get('e') != 'kline':
            return

        k = msg['k']

        if not k['x']:
            return  # wait for candle close

        candle = {
            "open": float(k['o']),
            "high": float(k['h']),
            "low": float(k['l']),
            "close": float(k['c']),
            "volume": float(k['v'])
        }

        ind = update_indicators(candle)

        print(f"📊 Close: {ind['close']} | RSI: {ind['RSI']:.2f}")

    except Exception as e:
        log_error(f"Socket error: {str(e)}")

# =============================
# WATCHDOG (AUTO RECONNECT)
# =============================
def watchdog():
    global last_msg_time

    while True:
        if time.time() - last_msg_time > 60:
            log_error("⚠️ No data received for 60s (socket issue)")
            print("⚠️ Socket seems dead. Restarting...")

            os._exit(1)  # restart externally (best practice)

        time.sleep(10)

# =============================
# MAIN
# =============================
if __name__ == "__main__":
    print("🚀 Bot Started")

    load_history()

    print("🔌 Starting WebSocket...")

    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()

    twm.start_kline_socket(
        callback=handle_socket,
        symbol=SYMBOL_UPPER,
        interval=INTERVAL
    )

    print("✅ WebSocket Started")

    # Start watchdog
    import threading
    threading.Thread(target=watchdog, daemon=True).start()

    while True:
        time.sleep(2)