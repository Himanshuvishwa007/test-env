"""
TestBot — Strategy Testing and Dataset Generation Engine for ProBot.
Uses Binance Futures Testnet API with automatic paper-simulation fallback.
Generates comprehensive datasets (candles, signals, trades, and metrics)
for future strategy analysis, backtesting, and bot restructuring.
"""

import sys
import time
import signal
import logging
import argparse
import threading
from urllib.parse import urlparse
from datetime import datetime
from typing import List, Optional, Dict, Any

from modules.config import BotConfig, setup_logging
from modules.indicators import IndicatorTracker, IndicatorValues
from modules.exchange import BinanceExchange
from modules.strategy import MeanReversionStrategy, TradeTargets
from modules.recorder import DataRecorder, CandleRecord, TradeRecord

log = setup_logging(log_file="testbot.log")


class TestEngine:
    """
    Test execution engine for a single symbol.
    Supports both Binance Futures Testnet API and real-time Paper Trading simulation.
    """

    def __init__(
        self,
        symbol: str,
        config: BotConfig,
        exchange: Optional[BinanceExchange],
        recorder: DataRecorder,
        is_paper: bool = False,
        sim_balance: float = 1000.0
    ):
        self.symbol = symbol.upper()
        self.config = config
        self.exchange = exchange
        self.recorder = recorder
        self.is_paper = is_paper
        self.balance = sim_balance

        # Strategy and indicators
        self.indicators = IndicatorTracker(
            vwap_period=config.vwap_period,
            band_multiplier=config.vwap_band_mult,
            rsi_period=config.rsi_period,
            adx_period=config.adx_period
        )
        self.strategy = MeanReversionStrategy(config=config)

        # State tracking
        self.in_trade: bool = False
        self.cooldown: int = 0
        self.active_side: Optional[str] = None
        self.entry_price: float = 0.0
        self.tp_target: float = 0.0
        self.sl_target: float = 0.0
        self.position_qty: float = 0.0
        self.trade_duration_candles: int = 0

        # Testnet order IDs
        self.sl_algo_id: Optional[str] = None
        self.tp_algo_id: Optional[str] = None

        # Precision parameters
        self.step = 0.001
        self.tick = 0.1
        self.min_notional = 5.0

        if self.exchange:
            try:
                filters = self.exchange.get_symbol_filters(self.symbol)
                self.step = filters["step"]
                self.tick = filters["tick"]
                self.min_notional = filters["min_notional"]
            except Exception as e:
                log.warning(f"[{self.symbol}] Using default filters: {e}")

        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_candle_time: float = time.time()
        self.last_tick_time: float = 0.0

    def preload(self) -> None:
        """Preloads historical klines to initialize indicator buffers."""
        log.info(f"[{self.symbol}] Preloading {self.config.vwap_period} historical candles for indicators warmup...")
        try:
            if self.exchange:
                klines = self.exchange.fetch_klines(
                    symbol=self.symbol,
                    interval=self.config.interval,
                    limit=self.config.vwap_period
                )
            else:
                import requests
                url = f"https://fapi.binance.com/fapi/v1/klines?symbol={self.symbol}&interval={self.config.interval}&limit={self.config.vwap_period}"
                klines = requests.get(url, timeout=10).json()

            vals = self.indicators.warmup(klines)
            vwap_str = f"{vals.vwap:.2f}" if vals.vwap else "NA"
            lower_b_str = f"{vals.lower_band:.2f}" if vals.lower_band else "NA"
            upper_b_str = f"{vals.upper_band:.2f}" if vals.upper_band else "NA"
            rsi_str = f"{vals.rsi:.1f}" if vals.rsi else "NA"
            adx_str = f"{vals.adx:.1f}" if vals.adx else "NA"

            log.info(
                f"[{self.symbol}] Warmup Complete | VWAP={vwap_str} | "
                f"Bands=[{lower_b_str} - {upper_b_str}] | RSI={rsi_str} | ADX={adx_str}"
            )
        except Exception as e:
            log.error(f"[{self.symbol}] Failed to preload historical data: {e}")
            raise

    def check_exit(self, high: float, low: float, close: float) -> None:
        """Evaluates Take Profit and Stop Loss triggers for active position."""
        if not self.in_trade:
            return

        self.trade_duration_candles += 1
        exit_reason = None
        exit_price = close

        if self.active_side == "BUY":
            if high >= self.tp_target:
                exit_reason = "TAKE_PROFIT"
                exit_price = self.tp_target
            elif low <= self.sl_target:
                exit_reason = "STOP_LOSS"
                exit_price = self.sl_target

        elif self.active_side == "SELL":
            if low <= self.tp_target:
                exit_reason = "TAKE_PROFIT"
                exit_price = self.tp_target
            elif high >= self.sl_target:
                exit_reason = "STOP_LOSS"
                exit_price = self.sl_target

        if exit_reason:
            self._close_trade(exit_price=exit_price, exit_reason=exit_reason)

    def _close_trade(self, exit_price: float, exit_reason: str) -> None:
        """Executes position closure, records trade analytics, and updates balance."""
        mode_str = "PAPER_SIMULATION" if self.is_paper else "TESTNET_API"

        if self.active_side == "BUY":
            gross = (exit_price - self.entry_price) * self.position_qty
        else:
            gross = (self.entry_price - exit_price) * self.position_qty

        entry_notional = self.entry_price * self.position_qty
        exit_notional = exit_price * self.position_qty
        fee = (entry_notional + exit_notional) * 0.0004
        net = gross - fee

        self.balance += net

        trade = self.recorder.record_trade_close(
            symbol=self.symbol,
            exit_price=exit_price,
            exit_reason=exit_reason,
            current_balance=self.balance,
            duration_candles=self.trade_duration_candles
        )

        pnl_pct = trade.pnl_pct if trade else 0.0
        log.info(
            f"[{self.symbol}] TRADE CLOSED ({exit_reason}) | Exit={exit_price:.2f} | "
            f"Net PnL={net:+.2f} USDT ({pnl_pct:+.2f}%) | Balance={self.balance:.2f} USDT | Mode={mode_str}"
        )

        if not self.is_paper and self.exchange:
            if self.sl_algo_id:
                self.exchange.cancel_algo_order(self.symbol, self.sl_algo_id)
                self.sl_algo_id = None
            if self.tp_algo_id:
                self.exchange.cancel_algo_order(self.symbol, self.tp_algo_id)
                self.tp_algo_id = None

        self.in_trade = False
        self.active_side = None
        self.entry_price = 0.0
        self.tp_target = 0.0
        self.sl_target = 0.0
        self.position_qty = 0.0
        self.trade_duration_candles = 0

    def open_trade(self, side: str, price: float, vwap_target: Optional[float] = None) -> bool:
        """Executes a new test trade and registers targets."""
        with self._lock:
            if self.in_trade or self.cooldown > 0:
                return False

            qty = self.strategy.calculate_quantity(
                balance=self.balance,
                price=price,
                step_size=self.step,
                min_notional=self.min_notional
            )

            if qty <= 0:
                log.warning(f"[{self.symbol}] Order quantity too small for balance ({self.balance:.2f} USDT)")
                return False

            targets: TradeTargets = self.strategy.calculate_targets(
                side=side,
                entry_price=price,
                tick_size=self.tick,
                vwap_target=vwap_target
            )

            mode_str = "PAPER_SIMULATION" if self.is_paper else "TESTNET_API"

            if not self.is_paper and self.exchange:
                try:
                    log.info(f"[{self.symbol}] Placing TESTNET {side} Market Order (Qty: {qty})...")
                    self.exchange.create_market_order(self.symbol, side, qty)
                    time.sleep(0.5)
                    entry = self.exchange.get_position_entry(self.symbol)
                    if entry and entry > 0:
                        targets = self.strategy.calculate_targets(side, entry, self.tick, vwap_target=vwap_target)

                    close_side = "SELL" if side == "BUY" else "BUY"
                    self.sl_algo_id = self.exchange.place_algo_order(
                        self.symbol, close_side, "STOP_MARKET", targets.stop_loss, qty
                    )
                    self.tp_algo_id = self.exchange.place_algo_order(
                        self.symbol, close_side, "TAKE_PROFIT_MARKET", targets.take_profit, qty
                    )
                except Exception as e:
                    log.error(f"[{self.symbol}] Testnet execution error: {e} — falling back to paper for this trade")
                    mode_str = "PAPER_SIMULATION"

            self.in_trade = True
            self.active_side = side
            self.entry_price = targets.entry_price
            self.tp_target = targets.take_profit
            self.sl_target = targets.stop_loss
            self.position_qty = qty
            self.cooldown = self.config.cooldown_candles
            self.trade_duration_candles = 0

            self.recorder.record_trade_open(
                symbol=self.symbol,
                side=side,
                entry_price=self.entry_price,
                quantity=qty,
                leverage=self.config.leverage,
                tp_price=self.tp_target,
                sl_price=self.sl_target,
                balance=self.balance,
                mode=mode_str
            )

            log.info(
                f"[{self.symbol}] TRADE OPENED ({side}) | Entry={self.entry_price:.2f} | "
                f"TP={self.tp_target:.2f} (+{targets.tp_pct*100:.2f}%) | "
                f"SL={self.sl_target:.2f} (-{targets.sl_pct*100:.2f}%) | "
                f"Qty={qty} | Mode={mode_str}"
            )
            return True

    def process_candle(
        self,
        timestamp: int,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float
    ) -> None:
        """Processes closed candle: checks exits, evaluates signal, records dataset."""
        # 1. Check exits first on this candle
        self.check_exit(high=high, low=low, close=close)

        # 2. Update indicators
        vals: IndicatorValues = self.indicators.update(
            close=close, volume=volume, high=high, low=low, open_price=open_price
        )

        dt_str = datetime.utcfromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")

        # 3. Analyze market state
        trend_bias = "BULLISH" if (vals.ema and close > vals.ema) else ("BEARISH" if vals.ema else "NA")
        vwap_delta_pct = ((close - vals.vwap) / vals.vwap * 100.0) if vals.vwap else None

        if vals.lower_band and vals.upper_band:
            if close <= vals.lower_band:
                vwap_status = "OVERSOLD_BAND"
            elif close >= vals.upper_band:
                vwap_status = "OVERBOUGHT_BAND"
            else:
                vwap_status = "INSIDE_BANDS"
        else:
            vwap_status = "WARMING"

        if vals.rsi is not None:
            if vals.rsi <= self.config.rsi_oversold:
                rsi_status = "OVERSOLD"
            elif vals.rsi >= self.config.rsi_overbought:
                rsi_status = "OVERBOUGHT"
            else:
                rsi_status = "NEUTRAL"
        else:
            rsi_status = "NA"

        # 4. Evaluate strategy signal
        signal = self.strategy.evaluate_signal(
            close=close, indicators=vals, open_price=open_price, high=high, low=low
        )
        action_taken = "WATCHING"

        if signal:
            if self.in_trade:
                action_taken = "IN_TRADE_HOLDING"
            elif self.cooldown > 0:
                action_taken = "COOLDOWN_SKIPPED"
            else:
                success = self.open_trade(side=signal, price=close, vwap_target=vals.vwap)
                action_taken = "ORDER_PLACED" if success else "SKIPPED_INSUFFICIENT_QTY"

        # 5. Record candle snapshot
        record = CandleRecord(
            timestamp=timestamp,
            datetime_str=dt_str,
            symbol=self.symbol,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            ema=vals.ema,
            rsi=vals.rsi,
            vwap=vals.vwap,
            vwap_delta_pct=vwap_delta_pct,
            trend_bias=trend_bias,
            vwap_status=vwap_status,
            rsi_status=rsi_status,
            signal=signal or "NONE",
            action_taken=action_taken,
            in_trade=self.in_trade,
            cooldown=self.cooldown
        )
        self.recorder.record_candle(record)

        # 6. Console log
        signal_display = f"*** {signal} ***" if signal else "NONE"
        vwap_str = f"{vals.vwap:.2f}" if vals.vwap else "NA"
        rsi_str = f"{vals.rsi:.1f}" if vals.rsi else "NA"
        adx_str = f"{vals.adx:.1f}" if vals.adx else "NA"
        bands_str = f"[{vals.lower_band:.2f}-{vals.upper_band:.2f}]" if vals.lower_band else "NA"

        log.info(
            f"[{self.symbol}] {dt_str} | Close={close:.2f} | "
            f"VWAP={vwap_str} | Bands={bands_str} | RSI={rsi_str} | ADX={adx_str} | "
            f"Signal={signal_display} | Action={action_taken}"
        )

        with self._lock:
            if self.cooldown > 0:
                self.cooldown -= 1


def run_historical_backtest(
    symbols: List[str],
    config: BotConfig,
    recorder: DataRecorder,
    candle_count: int = 500
) -> None:
    """
    Downloads historical candles from Binance REST API and runs instant simulation.
    Generates datasets and performance report in seconds.
    """
    import requests
    log.info(f"Starting instant backtest over past {candle_count} candles for {symbols}...")

    for sym in symbols:
        engine = TestEngine(
            symbol=sym,
            config=config,
            exchange=None,
            recorder=recorder,
            is_paper=True,
            sim_balance=recorder.metrics["current_balance"]
        )

        # Fetch candles (support up to 1500 per call)
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={config.interval}&limit={min(candle_count, 1500)}"
        try:
            resp = requests.get(url, timeout=10)
            klines = resp.json()
        except Exception as e:
            log.error(f"Failed to fetch historical klines for {sym}: {e}")
            continue

        if len(klines) < config.vwap_period:
            log.warning(f"Not enough candles fetched ({len(klines)} < {config.vwap_period}) for {sym}")
            continue

        warmup_slice = klines[:config.vwap_period]
        test_slice = klines[config.vwap_period:]

        log.info(f"[{sym}] Warming up with {len(warmup_slice)} candles...")
        engine.indicators.warmup(warmup_slice)

        log.info(f"[{sym}] Simulating {len(test_slice)} live candles...")
        for k in test_slice:
            ts = int(k[0])
            open_p = float(k[1])
            high = float(k[2])
            low = float(k[3])
            close = float(k[4])
            vol = float(k[5])

            engine.process_candle(
                timestamp=ts,
                open_price=open_p,
                high=high,
                low=low,
                close=close,
                volume=vol
            )

        recorder.metrics["current_balance"] = engine.balance

    print(recorder.get_summary_report())


def run_live_stream(
    symbols: List[str],
    config: BotConfig,
    exchange: Optional[BinanceExchange],
    recorder: DataRecorder,
    is_paper: bool = False
) -> None:
    """Runs real-time WebSocket testbot monitoring."""
    import websocket as ws_client
    import json

    engines: Dict[str, TestEngine] = {}
    threads: List[threading.Thread] = []
    shutdown_flag = threading.Event()

    for sym in symbols:
        eng = TestEngine(
            symbol=sym,
            config=config,
            exchange=exchange,
            recorder=recorder,
            is_paper=is_paper,
            sim_balance=recorder.metrics["current_balance"]
        )
        eng.preload()
        engines[sym] = eng

        def start_symbol_stream(symbol_name: str, test_engine: TestEngine):
            stream_url = f"{config.ws_base_url}/{symbol_name.lower()}@kline_{config.interval}"

            def _on_message(_ws, msg_str):
                try:
                    data = json.loads(msg_str)
                    if data.get("e") != "kline":
                        return
                    k = data["k"]

                    now = time.time()
                    if now - test_engine.last_tick_time > 15.0:
                        test_engine.last_tick_time = now
                        log.info(f"[{symbol_name}] Heartbeat tick | Price={k['c']} | WS Active")

                    if not k.get("x", False):
                        return

                    test_engine.process_candle(
                        timestamp=int(k["t"]),
                        open_price=float(k["o"]),
                        high=float(k["h"]),
                        low=float(k["l"]),
                        close=float(k["c"]),
                        volume=float(k["v"])
                    )
                except Exception as e:
                    log.error(f"[{symbol_name}] WS parse error: {e}")

            def _on_error(_ws, err):
                log.error(f"[{symbol_name}] WS error: {err}")

            def _on_open(_ws):
                log.info(f"[{symbol_name}] WebSocket stream connected to {stream_url}")

            # Configure keepalive ping/pong and optional proxy
            run_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 10,
            }
            if config.proxy:
                parsed = urlparse(config.proxy)
                if parsed.hostname:
                    run_kwargs["http_proxy_host"] = parsed.hostname
                    run_kwargs["http_proxy_port"] = parsed.port or 8080
                    if parsed.username:
                        run_kwargs["http_proxy_auth"] = (parsed.username, parsed.password or "")

            while not shutdown_flag.is_set():
                ws = ws_client.WebSocketApp(
                    stream_url,
                    on_message=_on_message,
                    on_error=_on_error,
                    on_open=_on_open
                )
                ws.run_forever(**run_kwargs)
                if not shutdown_flag.is_set():
                    time.sleep(3)

        t = threading.Thread(
            target=start_symbol_stream,
            args=(sym, eng),
            name=f"ws-{sym}",
            daemon=True
        )
        t.start()
        threads.append(t)
        time.sleep(1.0)

    def handle_shutdown(sig, frame):
        if not shutdown_flag.is_set():
            shutdown_flag.set()
            log.info("\nStopping TestBot... Flushing datasets and generating report...")

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        while not shutdown_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        handle_shutdown(None, None)

    print(recorder.get_summary_report())


def main():
    parser = argparse.ArgumentParser(description="ProBot Strategy Test & Dataset Generator")
    parser.add_argument("--symbols", nargs="+", default=["ETHUSDT"], help="Symbols to test (default: ETHUSDT)")
    parser.add_argument("--mode", choices=["testnet", "paper"], default="testnet", help="Execution mode")
    parser.add_argument("--backtest", type=int, default=0, help="Run instant simulation on N historical candles")
    parser.add_argument("--balance", type=float, default=1000.0, help="Initial simulation wallet balance")
    parser.add_argument("--output-dir", default="data", help="Output directory for generated datasets")
    args = parser.parse_args()

    config = BotConfig(
        symbols=[s.upper() for s in args.symbols],
        trading_mode="testnet"
    )

    recorder = DataRecorder(output_dir=args.output_dir)
    recorder.metrics["initial_balance"] = args.balance
    recorder.metrics["current_balance"] = args.balance

    exchange = None
    is_paper = (args.mode == "paper")

    if not is_paper:
        log.info("Testing Binance Futures Testnet API credentials...")
        test_exchange = BinanceExchange(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url,
            testnet=True,
            proxy=config.proxy
        )
        try:
            bal = test_exchange.get_balance()
            if bal > 0:
                log.info(f"Testnet API Authenticated successfully! Available Balance: {bal:.2f} USDT")
                exchange = test_exchange
                recorder.metrics["initial_balance"] = bal
                recorder.metrics["current_balance"] = bal
            else:
                log.warning("Testnet account balance is 0 or unavailable. Falling back to Paper Simulation mode.")
                is_paper = True
        except Exception as e:
            log.warning(f"Testnet authentication failed ({e}). Auto-falling back to High-Fidelity Paper Simulation mode.")
            is_paper = True

    # Mode 1: Instant Historical Backtest & Dataset Generation
    if args.backtest > 0:
        run_historical_backtest(
            symbols=config.symbols,
            config=config,
            recorder=recorder,
            candle_count=args.backtest
        )
        return

    # Mode 2: Real-time Live Stream Testing
    mode_label = "PAPER SIMULATION (Live Market Data)" if is_paper else "BINANCE FUTURES TESTNET (Live Orders)"
    log.info("=" * 65)
    log.info("         ProBot Strategy Test & Data Collector Started        ")
    log.info("=" * 65)
    log.info(f" Mode            : {mode_label}")
    log.info(f" Symbols         : {', '.join(config.symbols)}")
    log.info(f" Timeframe       : {config.interval}")
    log.info(f" Leverage        : {config.leverage}x")
    log.info(f" Data Output Dir : {args.output_dir}/")
    log.info("=" * 65)

    run_live_stream(
        symbols=config.symbols,
        config=config,
        exchange=exchange,
        recorder=recorder,
        is_paper=is_paper
    )


if __name__ == "__main__":
    main()
