"""
ProBot modular trading system.
Provides modules for configuration, technical indicators, exchange interaction,
trading strategies, and execution engine.
"""

from modules.config import BotConfig, setup_logging
from modules.indicators import IndicatorTracker, IndicatorValues
from modules.exchange import BinanceExchange
from modules.strategy import MeanReversionStrategy, TradeTargets
from modules.engine import ProBot
from modules.recorder import DataRecorder, CandleRecord, TradeRecord

__all__ = [
    "BotConfig",
    "setup_logging",
    "IndicatorTracker",
    "IndicatorValues",
    "BinanceExchange",
    "MeanReversionStrategy",
    "TradeTargets",
    "ProBot",
    "DataRecorder",
    "CandleRecord",
    "TradeRecord",
]

