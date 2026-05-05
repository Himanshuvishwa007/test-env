import os
import time
import csv
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from collections import deque

from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager

# =============================
# CONFIG
# =============================
load_dotenv()

API_KEY = os.getenv("BINANCE_LIVE_API_KEY")
API_SECRET = os.getenv("BINANCE_LIVE_API_SECRET")

SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_5MINUTE

LEVERAGE = 3
EMA_PERIOD = 198
ATR_PERIOD = 14
RSI_PERIOD = 14
VWAP_PERIOD = 288   # ✅ 24h rolling VWAP
COOLDOWN = 2

# =============================
# LOGGING
# =============================
log = logging.getLogger()
log.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(message)s")

console = logging.StreamHandler()
console.setFormatter(formatter)
log.addHandler(console)

file_handler = logging.FileHandler("bot.log")
file_handler.setFormatter(formatter)
log.addHandler(file_handler)

# =============================
class ProBot:

    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)

        self.ema = None
        self.prev_close = None

        self.atr = None
        self.tr_queue = deque(maxlen=ATR_PERIOD)
        self.rsi_queue = deque(maxlen=RSI_PERIOD)

        # ✅ Rolling VWAP
        self.vwap_queue = deque(maxlen=VWAP_PERIOD)
        self.vwap_pv_sum = 0
        self.vwap_vol_sum = 0

        self.cooldown = 0
        self.alpha = 2 / (EMA_PERIOD + 1)

        self.csv_file = f"trades_{SYMBOL}.csv"

        try:
            with open(self.csv_file, "x", newline="") as f:
                csv.writer(f).writerow(["time","side","entry","tp","sl","qty"])
        except:
            pass

        # Precision
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == SYMBOL:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self.step = float(f["stepSize"])
                    if f["filterType"] == "PRICE_FILTER":
                        self.tick = float(f["tickSize"])
                    if f["filterType"] == "MIN_NOTIONAL":
                        self.min_notional = float(f["notional"])
                break

    # =============================
    def round_qty(self, q):
        return float(Decimal(q).quantize(Decimal(str(self.step)), ROUND_DOWN))

    def round_price(self, p):
        return float(Decimal(p).quantize(Decimal(str(self.tick)), ROUND_DOWN))

    def get_balance(self):
        try:
            return float(self.client.futures_account().get("availableBalance", 0))
        except:
            return 0

    def get_position(self):
        try:
            pos = self.client.futures_position_information(symbol=SYMBOL)
            return float(pos[0].get("positionAmt", 0)) if pos else 0
        except:
            return 0

    # =============================
    def update_indicators(self, close, high, low, volume):

        if self.ema is None:
            self.ema = close
            self.prev_close = close
            return None, None, None, None

        # EMA
        self.ema += self.alpha * (close - self.ema)

        # ATR
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.tr_queue.append(tr)
        if len(self.tr_queue) == ATR_PERIOD:
            self.atr = sum(self.tr_queue) / ATR_PERIOD

        # RSI
        change = close - self.prev_close
        self.rsi_queue.append(change)

        if len(self.rsi_queue) == RSI_PERIOD:
            gains = sum(x for x in self.rsi_queue if x > 0)
            losses = abs(sum(x for x in self.rsi_queue if x < 0))
            if losses == 0:
                rsi = 100
            else:
                rs = gains / losses
                rsi = 100 - (100 / (1 + rs))
        else:
            rsi = None

        # =============================
        # ✅ Rolling VWAP (288)
        # =============================
        pv = close * volume

        if len(self.vwap_queue) == self.vwap_queue.maxlen:
            old_pv, old_vol = self.vwap_queue[0]
            self.vwap_pv_sum -= old_pv
            self.vwap_vol_sum -= old_vol

        self.vwap_queue.append((pv, volume))
        self.vwap_pv_sum += pv
        self.vwap_vol_sum += volume

        vwap = self.vwap_pv_sum / self.vwap_vol_sum if self.vwap_vol_sum > 0 else None

        self.prev_close = close

        return self.ema, self.atr, rsi, vwap

    # =============================
    def preload_data(self):
        log.info("📊 Preloading VWAP (288 candles)...")

        klines = self.client.futures_klines(
            symbol=SYMBOL,
            interval=INTERVAL,
            limit=VWAP_PERIOD
        )

        for k in klines:
            close = float(k[4])
            high = float(k[2])
            low = float(k[3])
            volume = float(k[5])

            self.update_indicators(close, high, low, volume)

        log.info("✅ Preload complete")

    # =============================
    def place_trade(self, side, price, atr):

        if self.cooldown > 0 or self.get_position() != 0:
            return

        if atr is None or atr <= 0:
            return

        balance = self.get_balance()
        if balance < 5:
            return

        qty = self.round_qty((balance * LEVERAGE * 0.9) / price)
        if qty * price < self.min_notional:
            return

        opposite = "SELL" if side == "BUY" else "BUY"

        try:
            # ENTRY
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=qty
            )

            time.sleep(0.5)

            pos = self.client.futures_position_information(symbol=SYMBOL)
            if not pos:
                return

            entry = float(pos[0].get("entryPrice", 0))
            if entry == 0:
                return

            sl = entry - atr if side == "BUY" else entry + atr
            tp = entry + atr * 1.5 if side == "BUY" else entry - atr * 1.5

            sl = self.round_price(sl)
            tp = self.round_price(tp)

            log.info(f"{side} | Entry={entry:.2f} TP={tp:.2f} SL={sl:.2f}")

            # STOP LOSS
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="STOP_MARKET",
                stopPrice=sl,
                quantity=qty,
                reduceOnly=True,
                workingType="MARK_PRICE"
            )

            # TAKE PROFIT
            self.client.futures_create_order(
                symbol=SYMBOL,
                side=opposite,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                quantity=qty,
                reduceOnly=True,
                workingType="MARK_PRICE"
            )

            self.cooldown = COOLDOWN

        except Exception as e:
            log.error(f"TRADE ERROR: {e}")

    # =============================
    def strategy(self, close, ema, atr, rsi, vwap):

        if None in (ema, atr, rsi, vwap):
            return

        if close > ema and close > vwap and rsi > 50:
            self.place_trade("BUY", close, atr)

        elif close < ema and close < vwap and rsi < 50:
            self.place_trade("SELL", close, atr)

    # =============================
    def on_message(self, msg):
        try:
            if not isinstance(msg, dict):
                return

            if msg.get("e") != "kline":
                return

            k = msg.get("k")
            if not k or not k.get("x"):
                return

            close = float(k["c"])
            high = float(k["h"])
            low = float(k["l"])
            volume = float(k["v"])

            ema, atr, rsi, vwap = self.update_indicators(
                close, high, low, volume
            )

            log.info(
                f"CLOSE={close:.2f} | "
                f"EMA={round(ema,2) if ema else 'NA'} | "
                f"RSI={round(rsi,2) if rsi else 'NA'} | "
                f"VWAP={round(vwap,2) if vwap else 'NA'} | "
                f"ATR={round(atr,2) if atr else 'NA'}"
            )

            self.strategy(close, ema, atr, rsi, vwap)

            if self.cooldown > 0:
                self.cooldown -= 1

        except Exception as e:
            log.error(f"WS ERROR: {e} | MSG: {msg}")

    # =============================
    def run(self):

        self.client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        self.preload_data()

        twm = ThreadedWebsocketManager(API_KEY, API_SECRET)
        twm.start()

        twm.start_kline_socket(
            callback=self.on_message,
            symbol=SYMBOL,
            interval=INTERVAL
        )

        log.info("🚀 BOT STARTED")

        while True:
            time.sleep(1)


if __name__ == "__main__":
    ProBot().run()