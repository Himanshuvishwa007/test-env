"""
Configuration and logging setup for ProBot.
Handles environment variables, trading parameters, and structured logging.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass
class BotConfig:
    """Trading bot configuration settings."""

    # Primary Trading Symbol
    symbols: List[str] = field(default_factory=lambda: ["ETHUSDT"])

    # Kline interval (5m default)
    interval: str = "5m"

    # Leverage applied per symbol
    leverage: int = 3

    # Statistical VWAP & Band Parameters
    vwap_period: int = 288         # 24-hour rolling window on 5m candles
    vwap_band_mult: float = 1.5    # Enter when price breaks out >= 1.5 std deviations from VWAP

    # Momentum Confirmation Filter (RSI)
    rsi_period: int = 14
    rsi_min: float = 50.0          # RSI must be >= 50 for Long breakout
    rsi_max: float = 50.0          # RSI must be <= 50 for Short breakout
    rsi_oversold: float = 50.0     # Backward compatibility alias
    rsi_overbought: float = 50.0   # Backward compatibility alias

    # Market Regime Filter (ADX)
    adx_period: int = 14
    adx_max: float = 999.0         # Extreme runaway trend protection threshold (999 = off)

    # Price Action Filter
    wick_ratio: float = 0.25       # Minimum wick ratio if used

    # Risk Management & Stop Loss
    sl_pct: float = 0.010          # 1.0% tight, risk-managed stop loss
    tp_pct_min: float = 0.018      # 1.8% profit target (1.8:1 Risk:Reward ratio)
    cooldown_candles: int = 1      # 1 candle wait after trade exit

    # Capital Allocation
    min_wallet_balance: float = 5.0      # Minimum USDT required to trade
    wallet_risk_fraction: float = 0.9    # Fraction of available balance to use for margin

    # Resilience & Watchdog
    watchdog_timeout_sec: int = 90       # Reconnect WS if no data received in 90 seconds
    position_poll_interval: float = 3.0  # Seconds between background position checks

    # Binance API & Endpoints
    trading_mode: str = "live"
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.binance.com"
    ws_base_url: str = "wss://fstream.binance.com/ws"
    proxy: Optional[str] = None

    def __post_init__(self):
        # Determine trading mode from env if not explicitly passed
        env_mode = os.getenv("TRADING_MODE", "live").strip().lower()
        if not self.trading_mode or self.trading_mode == "live":
            self.trading_mode = env_mode

        # Proxy support (e.g. http://ip:port or socks5://ip:port)
        self.proxy = self.proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("BINANCE_PROXY")

        # Set endpoints and API keys based on mode
        if self.trading_mode == "testnet":
            self.api_key = self.api_key or os.getenv("BINANCE_TEST_API_KEY", "")
            self.api_secret = self.api_secret or os.getenv("BINANCE_TEST_API_SECRET", "")
            self.base_url = "https://testnet.binancefuture.com"
            self.ws_base_url = "wss://stream.binancefuture.com/ws"
        else:
            self.trading_mode = "live"
            self.api_key = self.api_key or os.getenv("BINANCE_LIVE_API_KEY", "")
            self.api_secret = self.api_secret or os.getenv("BINANCE_LIVE_API_SECRET", "")
            self.base_url = "https://fapi.binance.com"
            self.ws_base_url = "wss://fstream.binance.com/ws"


def setup_logging(log_file: str = "bot.log", level: int = logging.INFO) -> logging.Logger:
    """
    Configures unified logging with console (UTF-8) and rotating file handlers.
    Avoids duplicate handlers on repeated calls.
    """
    logger = logging.getLogger("ProBot")
    logger.setLevel(level)

    # Reconfigure stdout to utf-8 if supported to prevent Windows charmap encoding errors
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)

        # Rotating file handler (max 10 MB per file, keeps 5 backups)
        file_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
