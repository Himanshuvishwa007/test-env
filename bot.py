"""
ProBot — Automated Binance USDT-M Perpetual Futures Trading Bot.
Mean Reversion Strategy (EMA Trend + VWAP Pullback + RSI Filter).
Modular Architecture with Live/Testnet Support and Resilient Execution.
"""

import sys
import time
import signal
import logging
import threading
import argparse
from typing import List

from modules.config import BotConfig, setup_logging
from modules.exchange import BinanceExchange
from modules.engine import ProBot

# Initialize global logger
log = setup_logging(log_file="bot.log")

# Module-level defaults for backward compatibility
_DEFAULT_CONFIG = BotConfig()
SYMBOLS = _DEFAULT_CONFIG.symbols
INTERVAL = _DEFAULT_CONFIG.interval
LEVERAGE = _DEFAULT_CONFIG.leverage
RSI_PERIOD = _DEFAULT_CONFIG.rsi_period
VWAP_PERIOD = _DEFAULT_CONFIG.vwap_period
VWAP_BAND_MULT = _DEFAULT_CONFIG.vwap_band_mult
ADX_MAX = _DEFAULT_CONFIG.adx_max
COOLDOWN = _DEFAULT_CONFIG.cooldown_candles
SL_PCT = _DEFAULT_CONFIG.sl_pct
BASE_URL = _DEFAULT_CONFIG.base_url


def parse_arguments() -> argparse.Namespace:
    """Parses optional command-line arguments."""
    parser = argparse.ArgumentParser(
        description="ProBot — Multi-Symbol Binance USDT-M Futures Trading Bot"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Trading symbols to run (default: ETHUSDT)"
    )
    parser.add_argument(
        "--mode",
        choices=["live", "testnet"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env variable)"
    )
    parser.add_argument(
        "--leverage",
        type=int,
        default=None,
        help="Account leverage multiplier (e.g. --leverage 3)"
    )
    parser.add_argument(
        "--feed",
        choices=["auto", "ws", "rest"],
        default=None,
        help="Data feed: 'auto' (WS with REST fallback), 'ws' (WebSocket), or 'rest' (pure REST polling)"
    )
    parser.add_argument(
        "--tp",
        type=float,
        default=None,
        help="Take profit percentage target (e.g. 0.012 or 1.2 for 1.2%%)"
    )
    parser.add_argument(
        "--sl",
        type=float,
        default=None,
        help="Stop loss percentage threshold (e.g. 0.009 or 0.9 for 0.9%%)"
    )
    return parser.parse_args()


def display_banner(config: BotConfig, active_symbols: List[str]) -> None:
    """Prints startup summary and operational parameters."""
    rr_str = f"{config.tp_pct_min / config.sl_pct:.1f}:1 R:R" if config.sl_pct else "NA"
    log.info("=" * 65)
    log.info("           ProBot Binance Futures Automated Trader            ")
    log.info("=" * 65)
    log.info(f" Mode            : {config.trading_mode.upper()}")
    log.info(f" Endpoint        : {config.base_url}")
    log.info(f" Symbols         : {', '.join(active_symbols)}")
    log.info(f" Timeframe       : {config.interval}")
    log.info(f" Leverage        : {config.leverage}x")
    log.info(f" Strategy        : VWAP Volatility Momentum Breakout")
    log.info(f" VWAP Bands      : {config.vwap_band_mult}x Standard Deviations (288-period)")
    log.info(f" Momentum Filter : RSI in [{config.rsi_min:.0f}-{config.rsi_max_long:.0f}] (Long), [{config.rsi_min_short:.0f}-{config.rsi_max:.0f}] (Short)")
    log.info(f" Risk Management : SL={config.sl_pct*100:.1f}% | TP={config.tp_pct_min*100:.1f}% ({rr_str})")
    log.info("=" * 65)


def main() -> None:
    """Main execution entrypoint."""
    args = parse_arguments()

    # Build bot configuration
    config = BotConfig()
    if args.symbols:
        config.symbols = [s.strip().upper() for s in args.symbols]
    if args.mode:
        config.trading_mode = args.mode
    if args.leverage:
        config.leverage = args.leverage
    if args.feed:
        config.data_feed = args.feed
    if args.tp is not None:
        config.tp_pct_min = args.tp if args.tp < 1.0 else (args.tp / 100.0)
    if args.sl is not None:
        config.sl_pct = args.sl if args.sl < 1.0 else (args.sl / 100.0)

    # Validate API credentials
    if not config.api_key or not config.api_secret:
        log.error(
            f"API credentials missing for {config.trading_mode.upper()} mode! "
            f"Please check your .env file."
        )
        sys.exit(1)

    display_banner(config, config.symbols)

    # Initialize shared exchange client (caches symbol filters across bots)
    is_testnet = (config.trading_mode == "testnet")
    exchange = BinanceExchange(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url,
        testnet=is_testnet,
        proxy=config.proxy
    )

    # Check connection and balance
    balance = exchange.get_balance()
    log.info(f"Account Balance: {balance:.2f} USDT")

    # Instantiate bots and threads
    bots: List[ProBot] = []
    threads: List[threading.Thread] = []

    for sym in config.symbols:
        bot = ProBot(symbol=sym, config=config, exchange=exchange)
        bots.append(bot)

        t = threading.Thread(
            target=bot.run,
            name=f"bot-{sym}",
            daemon=False
        )
        threads.append(t)
        t.start()
        time.sleep(1.5)  # Stagger startup to respect API rate limits

    # Graceful shutdown handler
    shutdown_flag = threading.Event()

    def handle_signal(sig, frame):
        if not shutdown_flag.is_set():
            shutdown_flag.set()
            log.info("\nShutdown signal received! Stopping all bots gracefully...")
            for b in bots:
                b.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Keep main thread alive and monitor workers
    try:
        while not shutdown_flag.is_set():
            alive_threads = [t for t in threads if t.is_alive()]
            if not alive_threads:
                log.info("All bot threads have finished.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        handle_signal(None, None)

    # Join threads with timeout
    for t in threads:
        t.join(timeout=3.0)

    log.info("ProBot shutdown complete. Goodbye.")


if __name__ == "__main__":
    main()