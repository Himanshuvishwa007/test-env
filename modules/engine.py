"""
ProBot trading engine module.
Orchestrates WebSocket data streaming, indicator updates, position monitoring,
and trade execution for a single trading pair.
"""

import time
import json
import logging
import threading
from typing import Optional

import websocket as ws_client
from binance.client import Client

from modules.config import BotConfig
from modules.indicators import IndicatorTracker, IndicatorValues
from modules.exchange import BinanceExchange
from modules.strategy import MeanReversionStrategy, TradeTargets

log = logging.getLogger("ProBot")


class ProBot:
    """
    Autonomous trading bot instance for a single symbol.
    Runs WebSocket feed, real-time indicator tracking, and risk-managed execution.
    """

    def __init__(
        self,
        symbol: str,
        config: BotConfig,
        exchange: BinanceExchange
    ):
        self.symbol = symbol.upper()
        self.config = config
        self.exchange = exchange

        # Core modules
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
        self.sl_algo_id: Optional[str] = None
        self.tp_algo_id: Optional[str] = None

        # Concurrency & lifecycle
        self._trade_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_candle_time: float = time.time()
        self._ws: Optional[ws_client.WebSocketApp] = None

        # Precision parameters (cached from exchange)
        filters = self.exchange.get_symbol_filters(self.symbol)
        self.step: float = filters["step"]
        self.tick: float = filters["tick"]
        self.min_notional: float = filters["min_notional"]

    def cancel_all_algo_orders(self) -> None:
        """Cancels active TP and SL algo orders for this symbol."""
        orders_to_cancel = [self.sl_algo_id, self.tp_algo_id]
        self.sl_algo_id = None
        self.tp_algo_id = None
        self.exchange.cancel_all_algo_orders(self.symbol, orders_to_cancel)

    def position_monitor(self) -> None:
        """
        Background daemon thread.
        Polls position status and cancels orphan TP/SL orders when a trade closes.
        """
        log.info(f"[{self.symbol}] Position monitor started")
        while not self.stop_event.is_set():
            try:
                amt = self.exchange.get_position(self.symbol)
                with self._trade_lock:
                    if self.in_trade and amt == 0:
                        log.info(f"[{self.symbol}] Position closed — cancelling orphan algo orders")
                        self.cancel_all_algo_orders()
                        self.in_trade = False
            except Exception as e:
                log.warning(f"[{self.symbol}] Monitor check error: {e}")

            self.stop_event.wait(self.config.position_poll_interval)

        log.info(f"[{self.symbol}] Position monitor stopped")

    def preload_data(self) -> None:
        """Preloads historical klines to initialize indicator buffers."""
        log.info(f"[{self.symbol}] Preloading {self.config.vwap_period} historical candles ({self.config.interval})...")
        try:
            klines = self.exchange.fetch_klines(
                symbol=self.symbol,
                interval=self.config.interval,
                limit=self.config.vwap_period
            )
            vals = self.indicators.warmup(klines)
            vwap_str = f"{vals.vwap:.2f}" if vals.vwap else "NA"
            band_str = f"[{vals.lower_band:.2f} - {vals.upper_band:.2f}]" if vals.lower_band else "NA"
            rsi_str = f"{vals.rsi:.1f}" if vals.rsi else "NA"
            adx_str = f"{vals.adx:.1f}" if vals.adx else "NA"
            log.info(
                f"[{self.symbol}] Preload complete — VWAP={vwap_str} | Bands={band_str} | RSI={rsi_str} | ADX={adx_str}"
            )
        except Exception as e:
            log.error(f"[{self.symbol}] Failed to preload historical data: {e}")
            raise

    def place_trade(self, side: str, price: float, vwap_target: Optional[float] = None) -> None:
        """
        Executes a market entry order and attaches conditional TP and SL algo orders.
        """
        with self._trade_lock:
            if self.in_trade:
                return
            if self.cooldown > 0:
                log.info(f"[{self.symbol}] In cooldown ({self.cooldown} candles remaining), skipping trade")
                return

            if self.exchange.get_position(self.symbol) != 0:
                self.in_trade = True
                return

            balance = self.exchange.get_balance()
            qty = self.strategy.calculate_quantity(
                balance=balance,
                price=price,
                step_size=self.step,
                min_notional=self.min_notional
            )

            if qty <= 0:
                log.warning(f"[{self.symbol}] Insufficient balance or quantity for trade (Balance: {balance:.2f} USDT)")
                return

            close_side = "SELL" if side == "BUY" else "BUY"

            try:
                log.info(f"[{self.symbol}] Executing {side} market order for {qty} units...")
                self.exchange.create_market_order(
                    symbol=self.symbol,
                    side=side,
                    qty=qty
                )

                time.sleep(0.5)

                entry = self.exchange.get_position_entry(self.symbol)
                if not entry or entry == 0:
                    log.error(f"[{self.symbol}] Could not fetch entry price — aborting SL/TP order placement")
                    return

                targets: TradeTargets = self.strategy.calculate_targets(
                    side=side,
                    entry_price=entry,
                    tick_size=self.tick,
                    vwap_target=vwap_target
                )

                log.info(
                    f"[{self.symbol}] {side} FILLED | Entry={targets.entry_price:.2f} | "
                    f"TP={targets.take_profit:.2f} (+{targets.tp_pct*100:.2f}%) | "
                    f"SL={targets.stop_loss:.2f} (-{targets.sl_pct*100:.2f}%) | Qty={qty}"
                )

                # Place Stop Loss algo order
                self.sl_algo_id = self.exchange.place_algo_order(
                    symbol=self.symbol,
                    side=close_side,
                    order_type="STOP_MARKET",
                    trigger_price=targets.stop_loss,
                    qty=qty
                )
                log.info(f"[{self.symbol}] SL algo order placed: ID={self.sl_algo_id}")

                # Place Take Profit algo order
                self.tp_algo_id = self.exchange.place_algo_order(
                    symbol=self.symbol,
                    side=close_side,
                    order_type="TAKE_PROFIT_MARKET",
                    trigger_price=targets.take_profit,
                    qty=qty
                )
                log.info(f"[{self.symbol}] TP algo order placed: ID={self.tp_algo_id}")

                self.in_trade = True
                self.cooldown = self.config.cooldown_candles

            except Exception as e:
                log.error(f"[{self.symbol}] TRADE ERROR: {e}")
                self.cancel_all_algo_orders()

    def on_kline(self, open_p: float, high: float, low: float, close: float, volume: float) -> None:
        """Handles a newly closed candle."""
        vals = self.indicators.update(
            close=close, volume=volume, high=high, low=low, open_price=open_p
        )

        vwap_str = f"{vals.vwap:.2f}" if vals.vwap else "NA"
        rsi_str = f"{vals.rsi:.1f}" if vals.rsi else "NA"
        adx_str = f"{vals.adx:.1f}" if vals.adx else "NA"
        lower_b_str = f"{vals.lower_band:.2f}" if vals.lower_band else "NA"
        upper_b_str = f"{vals.upper_band:.2f}" if vals.upper_band else "NA"

        log.info(
            f"[{self.symbol}] CLOSE={close:.2f} | VWAP={vwap_str} | "
            f"Bands=[{lower_b_str} - {upper_b_str}] | RSI={rsi_str} | ADX={adx_str} | "
            f"IN_TRADE={self.in_trade}"
        )

        signal = self.strategy.evaluate_signal(
            close=close, indicators=vals, open_price=open_p, high=high, low=low
        )
        if signal:
            log.info(f"[{self.symbol}] *** TRIGGER {signal} SIGNAL ***")
            self.place_trade(side=signal, price=close, vwap_target=vals.vwap)

        with self._trade_lock:
            if self.cooldown > 0:
                self.cooldown -= 1

    def on_ws_message(self, message: str) -> None:
        """Parses Binance WebSocket message."""
        try:
            msg = json.loads(message)
            self.last_candle_time = time.time()

            if msg.get("e") == "kline":
                k = msg["k"]
                if not k.get("x", False):
                    return

                open_p = float(k["o"])
                high = float(k["h"])
                low = float(k["l"])
                close = float(k["c"])
                volume = float(k["v"])

                self.on_kline(open_p=open_p, high=high, low=low, close=close, volume=volume)

        except Exception as e:
            log.error(f"[{self.symbol}] WS parse error: {e}")

    def run(self) -> None:
        """Main execution loop."""
        log.info(f"[{self.symbol}] Initializing leverage to {self.config.leverage}x...")
        self.exchange.set_leverage(symbol=self.symbol, leverage=self.config.leverage)
        self.preload_data()

        monitor_thread = threading.Thread(
            target=self.position_monitor,
            name=f"monitor-{self.symbol}",
            daemon=True
        )
        monitor_thread.start()

        log.info(
            f"[{self.symbol}] BOT STARTED | Strategy=VWAP Volatility Momentum Breakout | "
            f"Bands={self.config.vwap_band_mult}x Std | SL={self.config.sl_pct*100:.1f}% | "
            f"TP={self.config.tp_pct_min*100:.1f}% (1.8:1 R:R)"
        )

        stream_name = f"{self.symbol.lower()}@kline_{self.config.interval}"
        ws_url = f"{self.config.ws_base_url}/{stream_name}"

        while not self.stop_event.is_set():
            self._ws = None
            try:
                def _on_message(_ws, message):
                    self.on_ws_message(message)

                def _on_error(_ws, error):
                    log.error(f"[{self.symbol}] WS error: {error}")

                def _on_open(_ws):
                    log.info(f"[{self.symbol}] WebSocket connected ({stream_name})")

                def _on_close(_ws, status_code, msg):
                    log.info(f"[{self.symbol}] WebSocket closed (Code: {status_code}, Msg: {msg})")

                self._ws = ws_client.WebSocketApp(
                    ws_url,
                    on_message=_on_message,
                    on_error=_on_error,
                    on_open=_on_open,
                    on_close=_on_close,
                )

                wst = threading.Thread(
                    target=self._ws.run_forever,
                    name=f"ws-{self.symbol}",
                    daemon=True
                )
                wst.start()
                self.last_candle_time = time.time()

                while not self.stop_event.is_set():
                    time.sleep(10)
                    elapsed = time.time() - self.last_candle_time
                    if elapsed > self.config.watchdog_timeout_sec:
                        log.warning(
                            f"[{self.symbol}] No candle received in {int(elapsed / 60)} minutes — reconnecting..."
                        )
                        break
                    if not wst.is_alive():
                        log.warning(f"[{self.symbol}] WebSocket thread died — reconnecting...")
                        break

            except Exception as e:
                log.error(f"[{self.symbol}] WebSocket connection error: {e}")

            finally:
                if self._ws:
                    try:
                        self._ws.close()
                    except Exception:
                        pass

            if not self.stop_event.is_set():
                log.info(f"[{self.symbol}] Reconnecting WebSocket in 5 seconds...")
                self.stop_event.wait(5.0)

        log.info(f"[{self.symbol}] Bot run loop terminated")

    def stop(self) -> None:
        """Signals graceful shutdown."""
        log.info(f"[{self.symbol}] Stopping bot...")
        self.stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
