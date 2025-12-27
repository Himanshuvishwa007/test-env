from binance.client import Client
import pandas as pd
import time
import csv
from datetime import datetime

# =============================
# CONFIG
# =============================
API_KEY = "YMkkDGSM5Euk3NsAt21WcRhFFvm70559lMh1hsD79tQ0ayqPnctslCpFuEMA5R2yD"
API_SECRET = "oER1r73vKVvdWf5cJ0Hn7aOq4R1Pe3HAlciVgyur0TMZ9j0tqP5wUk1YwDLPazRD"

class BB_RSI_EMA200_PaperBot:

    def __init__(
        self,
        api_key,
        api_secret,
        symbol="BTCUSDT",
        interval=Client.KLINE_INTERVAL_5MINUTE,

        bb_window=20,
        bb_std=2.0,

        rsi_period=14,
        rsi_buy=40,
        rsi_sell=65,

        vol_window=20,
        vol_multiplier=1.1,

        ema_trend=200,

        tp_pct=0.02,
        sl_pct=0.02,
        fee=0.001,

        debug=True
    ):

        self.client = Client(api_key, api_secret, testnet=True)
        self.symbol = symbol
        self.interval = interval

        self.bb_window = bb_window
        self.bb_std = bb_std

        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell

        self.vol_window = vol_window
        self.vol_multiplier = vol_multiplier
        self.ema_trend = ema_trend

        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.fee = fee

        self.position = 0
        self.entry_price = None
        self.realized_pnl = 0

        self.data = pd.DataFrame()
        self.debug = debug

        with open("trade_log.csv", "w", newline="") as f:
            csv.writer(f).writerow(
                ["Time", "Action", "Price", "TradePnL", "NetPnL"]
            )

        print("✅ EMA200 + BB + RSI + Volume PAPER BOT STARTED")

    # =============================
    # RSI
    # =============================
    def compute_rsi(self, series):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(self.rsi_period).mean()
        avg_loss = loss.rolling(self.rsi_period).mean()

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    # =============================
    # Fetch Candle
    # =============================
    def fetch_candle(self):
        k = self.client.get_klines(
            symbol=self.symbol,
            interval=self.interval,
            limit=1
        )[0]

        return {
            "time": pd.to_datetime(k[0], unit="ms"),
            "close": float(k[4]),
            "volume": float(k[5])
        }

    # =============================
    # STRATEGY
    # =============================
    def process(self):
        candle = self.fetch_candle()
        self.data = pd.concat(
            [self.data, pd.DataFrame([candle])],
            ignore_index=True
        )

        min_len = max(
            self.bb_window,
            self.rsi_period,
            self.vol_window,
            self.ema_trend
        )

        if len(self.data) < min_len:
            return

        df = self.data.copy()

        # ---- Indicators ----
        df["RSI"] = self.compute_rsi(df["close"])
        df["VOL_SMA"] = df["volume"].rolling(self.vol_window).mean()

        sma = df["close"].rolling(self.bb_window).mean()
        std = df["close"].rolling(self.bb_window).std()
        df["BB_LOW"] = sma - self.bb_std * std

        df["EMA200"] = df["close"].ewm(
            span=self.ema_trend, adjust=False
        ).mean()

        last = df.iloc[-1]
        price = last["close"]

        # ================= BUY =================
        if self.position == 0:
            if (
                price > last["EMA200"] and
                last["RSI"] < self.rsi_buy and
                price < last["BB_LOW"] and
                last["volume"] > last["VOL_SMA"] * self.vol_multiplier
            ):
                self.position = 1
                self.entry_price = price
                self.log("BUY", price, 0)
                print(f"📈 BUY @ {price:.2f}")

        # ================= SELL =================
        else:
            tp = self.entry_price * (1 + self.tp_pct)
            sl = self.entry_price * (1 - self.sl_pct)

            exit_price = None
            reason = None

            if price >= tp:
                exit_price = price
                reason = "TAKE_PROFIT"
            elif price <= sl:
                exit_price = price
                reason = "STOP_LOSS"
            elif last["RSI"] > self.rsi_sell:
                exit_price = price
                reason = "RSI_EXIT"

            if reason:
                gross = exit_price - self.entry_price
                fee = (self.entry_price + exit_price) * self.fee
                pnl = gross - fee

                self.realized_pnl += pnl
                self.position = 0

                self.log(reason, exit_price, pnl)
                print(
                    f"💰 {reason} @ {exit_price:.2f} | "
                    f"PnL={pnl:.2f} | Net={self.realized_pnl:.2f}"
                )

        if self.debug:
            print(
                f"[{datetime.utcnow().strftime('%H:%M:%S')}] "
                f"P={price:.2f} | "
                f"RSI={last['RSI']:.1f} | "
                f"EMA200={last['EMA200']:.2f}"
            )

    def log(self, action, price, trade_pnl):
        with open("trade_log.csv", "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                action,
                round(price, 2),
                round(trade_pnl, 2),
                round(self.realized_pnl, 2)
            ])

    def run(self):
        print("\n🚀 Bot Running (Paper Trading)\n")
        while True:
            try:
                self.process()
                time.sleep(300)  # 5m candle
            except Exception as e:
                print("⚠️ Error:", e)
                time.sleep(10)

# =============================
# START
# =============================
bot = BB_RSI_EMA200_PaperBot(
    API_KEY,
    API_SECRET,
    symbol="BTCUSDT",
    interval=Client.KLINE_INTERVAL_5MINUTE,

    bb_window=20,
    bb_std=2.4,          # ✅ from grid

    rsi_period=14,
    rsi_buy=45,          # ✅ from grid
    rsi_sell=75,         # ✅ from grid

    vol_window=20,
    vol_multiplier=1.2, # ✅ from grid

    ema_trend=200,

    tp_pct=0.07,
    sl_pct=0.03,

    debug=True
)

bot.run()

