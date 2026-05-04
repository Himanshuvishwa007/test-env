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

SYMBOL = "ETHUSDT"
INTERVAL = "1m"

LEVERAGE = 2
RISK_PERCENT = 0.1

EMA_TREND = 198
RSI_PERIOD = 15
ATR_PERIOD = 15
VWAP_WINDOW = 51

TP_MULT = 1.5
SL_MULT = 1.0

client = Client(API_KEY, API_SECRET)

# =============================
# SET LEVERAGE
# =============================
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

# =============================
# FILTERS
# =============================
def get_filters():
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
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
    return float(Decimal(str(p)).quantize(Decimal(str(F["tick"])), ROUND_DOWN))

# =============================
# GLOBAL STATE
# =============================
ema = None
atr = None
rsi_avg_gain = None
rsi_avg_loss = None
prev_close = None

# FIX 3: Track VWAP sums alongside the deque to keep them consistent
vwap_queue = deque(maxlen=VWAP_WINDOW)
vwap_pv_sum = 0.0
vwap_vol_sum = 0.0

position = None  # Will be synced from Binance on startup

alpha = 2 / (EMA_TREND + 1)

# =============================
# FIX 2: SYNC POSITION FROM BINANCE
# =============================
def sync_position():
    """Read actual open position from Binance so restarts don't lose state."""
    global position
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt > 0:
                position = "LONG"
                print(f"⚠️  Existing LONG position detected ({amt} contracts). Will not open new trades until closed.")
                return
            elif amt < 0:
                position = "SHORT"
                print(f"⚠️  Existing SHORT position detected ({amt} contracts). Will not open new trades until closed.")
                return
        position = None
        print("✅ No open position. Ready to trade.")
    except Exception as e:
        print(f"❌ Could not sync position: {e}")
        position = None

# =============================
# FIX 1: RESET POSITION STATE
# Check Binance to see if our position was closed (by SL/TP/manually)
# =============================
def refresh_position_state():
    """Called before every trade check to detect if SL/TP fired."""
    global position
    if position is None:
        return
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        for p in positions:
            if float(p["positionAmt"]) != 0:
                return  # Still in a trade
        # If we reach here, Binance shows flat — reset our state
        print(f"✅ Position closed (SL/TP hit or manual). Ready for next trade.")
        position = None
    except Exception as e:
        print(f"⚠️  Could not refresh position state: {e}")

# =============================
# LOAD HISTORY
# =============================
def load_history():
    global ema, atr, rsi_avg_gain, rsi_avg_loss, prev_close
    global vwap_pv_sum, vwap_vol_sum

    # Fetch enough bars: max of all periods + buffer
    limit = max(EMA_TREND, RSI_PERIOD, ATR_PERIOD, VWAP_WINDOW) + 50
    klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=min(limit, 500))

    closes, highs, lows, volumes = [], [], [], []
    typical_prices = []  # FIX 5: Use (H+L+C)/3 for VWAP, not just close

    for k in klines:
        h, l, c, v = float(k[2]), float(k[3]), float(k[4]), float(k[5])
        closes.append(c)
        highs.append(h)
        lows.append(l)
        volumes.append(v)
        typical_prices.append((h + l + c) / 3)

    # --- EMA (Wilder-style warm-up: seed with first close, then iterate) ---
    ema_val = closes[0]
    for c in closes:
        ema_val = (c - ema_val) * alpha + ema_val
    ema = ema_val

    # --- ATR: FIX 4: Proper Wilder smoothing seed ---
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    # Seed with simple average of first ATR_PERIOD TRs, then Wilder-smooth the rest
    atr_val = sum(tr_list[:ATR_PERIOD]) / ATR_PERIOD
    for tr in tr_list[ATR_PERIOD:]:
        atr_val = ((atr_val * (ATR_PERIOD - 1)) + tr) / ATR_PERIOD
    atr = atr_val

    # --- RSI ---
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    rsi_avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    for i in range(RSI_PERIOD, len(gains)):
        rsi_avg_gain = ((rsi_avg_gain * (RSI_PERIOD - 1)) + gains[i]) / RSI_PERIOD
        rsi_avg_loss = ((rsi_avg_loss * (RSI_PERIOD - 1)) + losses[i]) / RSI_PERIOD

    # --- VWAP: FIX 3: Build queue cleanly, recompute sums from scratch ---
    vwap_queue.clear()
    vwap_pv_sum = 0.0
    vwap_vol_sum = 0.0

    recent_tp = typical_prices[-VWAP_WINDOW:]
    recent_vol = volumes[-VWAP_WINDOW:]

    for tp, v in zip(recent_tp, recent_vol):
        pv = tp * v
        vwap_queue.append((pv, v))
        vwap_pv_sum += pv
        vwap_vol_sum += v

    prev_close = closes[-1]

    print(f"✅ History Loaded | EMA={ema:.2f} | ATR={atr:.4f} | RSI_gain={rsi_avg_gain:.4f}")

# =============================
# INDICATORS
# =============================
def update_indicators(candle):
    global ema, atr, rsi_avg_gain, rsi_avg_loss, prev_close
    global vwap_pv_sum, vwap_vol_sum

    high, low, close, volume = candle
    typical_price = (high + low + close) / 3  # FIX 5: proper typical price

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

    rs = rsi_avg_gain / rsi_avg_loss if rsi_avg_loss != 0 else 100
    rsi = 100 - (100 / (1 + rs))

    # VWAP: FIX 3 — evict oldest entry's contribution before appending
    pv = typical_price * volume
    if len(vwap_queue) == VWAP_WINDOW:
        old_pv, old_vol = vwap_queue[0]   # peek at what deque will evict
        vwap_pv_sum -= old_pv
        vwap_vol_sum -= old_vol
    vwap_queue.append((pv, volume))
    vwap_pv_sum += pv
    vwap_vol_sum += volume

    vwap = vwap_pv_sum / vwap_vol_sum if vwap_vol_sum > 0 else close

    prev_close = close

    return close, ema, rsi, atr, vwap

# =============================
# POSITION SIZE
# =============================
def get_position_size(price):
    balance = float(client.futures_account()["totalWalletBalance"])
    risk_amount = balance * RISK_PERCENT
    qty = (risk_amount * LEVERAGE) / price
    qty = round_qty(qty)
    if qty < F["min_qty"]:
        print(f"⚠️  Calculated qty {qty} below min {F['min_qty']}. Skipping trade.")
        return None
    return qty

# =============================
# EXECUTION
# =============================
def place_order(side, qty, sl, tp):
    opposite = "SELL" if side == "BUY" else "BUY"
    try:
        # MARKET ENTRY
        client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=qty
        )
        print(f"✅ Market {side} order placed: {qty}")

        # STOP LOSS
        client.futures_create_order(
            symbol=SYMBOL,
            side=opposite,
            type="STOP_MARKET",
            stopPrice=round_price(sl),
            closePosition=True,
            timeInForce="GTC"
        )
        print(f"✅ SL placed at {round_price(sl)}")

        # TAKE PROFIT
        client.futures_create_order(
            symbol=SYMBOL,
            side=opposite,
            type="TAKE_PROFIT_MARKET",
            stopPrice=round_price(tp),
            closePosition=True,
            timeInForce="GTC"
        )
        print(f"✅ TP placed at {round_price(tp)}")

    except Exception as e:
        print(f"❌ Order placement failed: {e}")

# =============================
# STRATEGY
# =============================
def check_trade(close, ema_val, rsi, atr, vwap):
    global position

    # FIX 1: Always refresh position state from Binance before deciding
    refresh_position_state()

    if position is not None:
        return

    qty = get_position_size(close)
    if qty is None:
        return

    # LONG: price above EMA (uptrend), near/below VWAP (pullback), RSI not overbought
    if close > ema_val and close < vwap * 1.01 and rsi < 45:
        sl = close - atr * SL_MULT
        tp = close + atr * TP_MULT
        print(f"🚀 LONG  @ {close:.2f} | SL={sl:.2f} | TP={tp:.2f} | RSI={rsi:.1f} | ATR={atr:.4f} | VWAP={vwap:.2f}")
        place_order("BUY", qty, sl, tp)
        position = "LONG"

    # SHORT: price below EMA (downtrend), near/above VWAP (pullback), RSI not oversold
    elif close < ema_val and close > vwap * 0.99 and rsi > 55:
        sl = close + atr * SL_MULT
        tp = close - atr * TP_MULT
        print(f"🔻 SHORT @ {close:.2f} | SL={sl:.2f} | TP={tp:.2f} | RSI={rsi:.1f} | ATR={atr:.4f} | VWAP={vwap:.2f}")
        place_order("SELL", qty, sl, tp)
        position = "SHORT"

    else:
        print(f"⏳ No signal | Close={close:.2f} EMA={ema_val:.2f} VWAP={vwap:.2f} RSI={rsi:.1f}")

# =============================
# SOCKET
# =============================
def handle_socket(msg):
    try:
        if msg.get("e") != "kline":
            return

        k = msg["k"]

        if not k["x"]:
            return  # Only act on fully closed candles

        candle = (
            float(k["h"]),
            float(k["l"]),
            float(k["c"]),
            float(k["v"])
        )

        close, ema_val, rsi, atr, vwap = update_indicators(candle)

        print(f"📊 Close={close:.2f} | EMA={ema_val:.2f} | RSI={rsi:.1f} | ATR={atr:.4f} | VWAP={vwap:.2f} | Pos={position}")

        check_trade(close, ema_val, rsi, atr, vwap)

    except Exception as e:
        print(f"❌ Socket error: {e}")

# =============================
# MAIN
# =============================
if __name__ == "__main__":
    print("🚀 LIVE BOT STARTING...")

    load_history()
    sync_position()  # FIX 2: Sync position state from Binance before trading

    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()

    twm.start_kline_socket(
        callback=handle_socket,
        symbol=SYMBOL,
        interval=INTERVAL
    )

    print("🟢 Listening for candles...")
    while True:
        time.sleep(1)