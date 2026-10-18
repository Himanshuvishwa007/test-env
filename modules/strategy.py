"""
Trading strategy and signal calculation module for ProBot.

Strategy: High-Expectancy VWAP Volatility Momentum Breakout
  - Long (BUY):
      1. Candle close breaks above Upper Statistical Band (VWAP + band_mult * StdDev)
      2. Strong bullish expansion candle (Close > Open)
      3. Momentum confirmation (RSI >= 50 and Close > VWAP)
  - Short (SELL):
      1. Candle close breaks below Lower Statistical Band (VWAP - band_mult * StdDev)
      2. Strong bearish expansion candle (Close < Open)
      3. Momentum confirmation (RSI <= 50 and Close < VWAP)
  - Take Profit: Risk-adjusted target (1.8% minimum, 1.8:1 R:R)
  - Stop Loss: Noise-buffered stop (1.0%)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from modules.config import BotConfig
from modules.indicators import IndicatorValues

log = logging.getLogger("ProBot")


@dataclass
class TradeTargets:
    """Calculated stop loss and take profit price levels."""
    entry_price: float
    take_profit: float
    stop_loss: float
    tp_pct: float
    sl_pct: float


class VwapBreakoutStrategy:
    """
    High-Expectancy Statistical VWAP Volatility Momentum Breakout Strategy.
    Enters on institutional volatility expansion confirmed by momentum and candle structure.
    """

    def __init__(self, config: BotConfig):
        self.config = config

    def evaluate_signal(
        self,
        close: float,
        indicators: IndicatorValues,
        open_price: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None
    ) -> Optional[str]:
        """
        Evaluates a closed candle and returns a trade signal ('BUY', 'SELL', or None).
        """
        if not indicators.is_complete:
            return None

        # Extreme runaway trend protection if configured
        if (
            indicators.adx is not None and
            self.config.adx_max is not None and
            self.config.adx_max < 900.0 and
            indicators.adx >= self.config.adx_max
        ):
            return None

        open_p = open_price if open_price is not None else close
        high_p = high if high is not None else close
        low_p = low if low is not None else close

        rsi_val = indicators.rsi
        vwap_val = indicators.vwap
        upper_b = indicators.upper_band
        lower_b = indicators.lower_band

        if rsi_val is None or vwap_val is None or upper_b is None or lower_b is None:
            return None

        rsi_min_thresh = getattr(self.config, "rsi_min", 50.0)
        rsi_max_thresh = getattr(self.config, "rsi_max", 50.0)

        # ──────────────────────────────────────────────────────────────────────
        # LONG SIGNAL (Bullish Volatility Breakout):
        # 1. Close broke above Upper Statistical Band
        # 2. Bullish candle (close > open)
        # 3. Momentum confirmed: RSI >= 50 and price above VWAP
        # ──────────────────────────────────────────────────────────────────────
        is_long = (
            close > upper_b and
            close > open_p and
            rsi_val >= rsi_min_thresh and
            close > vwap_val
        )

        # ──────────────────────────────────────────────────────────────────────
        # SHORT SIGNAL (Bearish Volatility Breakdown):
        # 1. Close broke below Lower Statistical Band
        # 2. Bearish candle (close < open)
        # 3. Momentum confirmed: RSI <= 50 and price below VWAP
        # ──────────────────────────────────────────────────────────────────────
        is_short = (
            close < lower_b and
            close < open_p and
            rsi_val <= rsi_max_thresh and
            close < vwap_val
        )

        adx_str = f"{indicators.adx:.1f}" if indicators.adx is not None else "NA"

        if is_long:
            log.info(
                f"BUY Signal Triggered | Close={close:.2f} > UpperBand={upper_b:.2f} | "
                f"RSI={rsi_val:.1f} | VWAP={vwap_val:.2f} | ADX={adx_str}"
            )
            return "BUY"
        elif is_short:
            log.info(
                f"SELL Signal Triggered | Close={close:.2f} < LowerBand={lower_b:.2f} | "
                f"RSI={rsi_val:.1f} | VWAP={vwap_val:.2f} | ADX={adx_str}"
            )
            return "SELL"

        return None

    def calculate_targets(
        self,
        side: str,
        entry_price: float,
        tick_size: float,
        vwap_target: Optional[float] = None,
        atr: Optional[float] = None
    ) -> TradeTargets:
        """
        Calculates Take Profit and Stop Loss levels with tick precision.
        """
        tick_dec = Decimal(str(tick_size))
        entry_dec = Decimal(str(entry_price))
        sl_pct_dec = Decimal(str(self.config.sl_pct))
        tp_pct_dec = Decimal(str(self.config.tp_pct_min))

        if side == "BUY":
            raw_sl = entry_dec * (Decimal("1") - sl_pct_dec)
            # If explicit vwap_target provides good R:R, use it, else use configured TP %
            if vwap_target is not None and vwap_target > entry_price:
                gain = vwap_target - entry_price
                if gain >= float(entry_price * self.config.sl_pct * 1.5):
                    raw_tp = Decimal(str(vwap_target))
                else:
                    raw_tp = entry_dec * (Decimal("1") + tp_pct_dec)
            else:
                raw_tp = entry_dec * (Decimal("1") + tp_pct_dec)
        else:
            raw_sl = entry_dec * (Decimal("1") + sl_pct_dec)
            if vwap_target is not None and vwap_target < entry_price:
                gain = entry_price - vwap_target
                if gain >= float(entry_price * self.config.sl_pct * 1.5):
                    raw_tp = Decimal(str(vwap_target))
                else:
                    raw_tp = entry_dec * (Decimal("1") - tp_pct_dec)
            else:
                raw_tp = entry_dec * (Decimal("1") - tp_pct_dec)

        sl = float(raw_sl.quantize(tick_dec, rounding=ROUND_DOWN))
        tp = float(raw_tp.quantize(tick_dec, rounding=ROUND_DOWN))

        tp_pct = abs(tp - entry_price) / entry_price
        sl_pct = abs(entry_price - sl) / entry_price

        return TradeTargets(
            entry_price=entry_price,
            take_profit=tp,
            stop_loss=sl,
            tp_pct=round(tp_pct, 4),
            sl_pct=round(sl_pct, 4)
        )

    def calculate_quantity(
        self,
        balance: float,
        price: float,
        step_size: float,
        min_notional: float
    ) -> float:
        """
        Calculates order quantity based on available margin balance and leverage.
        """
        if balance < self.config.min_wallet_balance or price <= 0:
            return 0.0

        raw_qty = (balance * self.config.leverage * self.config.wallet_risk_fraction) / price
        step_dec = Decimal(str(step_size))
        qty = float(Decimal(str(raw_qty)).quantize(step_dec, rounding=ROUND_DOWN))

        if qty * price < min_notional:
            log.warning(
                f"Computed notional ({qty * price:.2f}) is below minimum notional ({min_notional:.2f})"
            )
            return 0.0

        return qty


# Backward compatibility alias
MeanReversionStrategy = VwapBreakoutStrategy
