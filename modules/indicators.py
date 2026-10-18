"""
Technical indicators calculation module for ProBot.
Computes incremental VWAP with Standard Deviation Bands, RSI, ADX (Market Regime),
and ATR without heavy external dependencies.
"""

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional, List, Tuple


@dataclass
class IndicatorValues:
    """Snapshot of technical indicator values for a candle."""
    vwap: Optional[float]
    vwap_std: Optional[float]
    upper_band: Optional[float]
    lower_band: Optional[float]
    rsi: Optional[float]
    adx: Optional[float]
    atr: Optional[float]
    ema: Optional[float]

    @property
    def is_complete(self) -> bool:
        """Returns True if core indicators (VWAP, Bands, RSI, ADX) are computed."""
        return (
            self.vwap is not None and
            self.upper_band is not None and
            self.lower_band is not None and
            self.rsi is not None and
            self.adx is not None
        )


class IndicatorTracker:
    """
    Stateful technical indicator calculator.
    Processes candles one-by-one with rolling queues and incremental formulas.
    """

    def __init__(
        self,
        vwap_period: int = 288,
        band_multiplier: float = 1.5,
        rsi_period: int = 14,
        adx_period: int = 14,
        atr_period: int = 14,
        ema_period: int = 200,
    ):
        self.vwap_period = vwap_period
        self.band_multiplier = band_multiplier
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.ema_period = ema_period
        self.alpha_ema = 2.0 / (ema_period + 1)

        # VWAP containers
        self.close_queue: deque = deque(maxlen=vwap_period)
        self.vwap_queue: deque = deque(maxlen=vwap_period)
        self.vwap_pv_sum: float = 0.0
        self.vwap_vol_sum: float = 0.0

        # RSI containers
        self.rsi_queue: deque = deque(maxlen=rsi_period)

        # ADX / ATR containers
        self.tr_queue: deque = deque(maxlen=adx_period)
        self.plus_dm_queue: deque = deque(maxlen=adx_period)
        self.minus_dm_queue: deque = deque(maxlen=adx_period)

        # State tracking
        self.prev_close: Optional[float] = None
        self.prev_high: Optional[float] = None
        self.prev_low: Optional[float] = None
        self.ema: Optional[float] = None
        self.candle_count: int = 0

    def reset(self) -> None:
        """Resets all indicator state."""
        self.close_queue.clear()
        self.vwap_queue.clear()
        self.vwap_pv_sum = 0.0
        self.vwap_vol_sum = 0.0
        self.rsi_queue.clear()
        self.tr_queue.clear()
        self.plus_dm_queue.clear()
        self.minus_dm_queue.clear()
        self.prev_close = None
        self.prev_high = None
        self.prev_low = None
        self.ema = None
        self.candle_count = 0

    def update(
        self,
        close: float,
        volume: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        open_price: Optional[float] = None
    ) -> IndicatorValues:
        """
        Updates indicators with a new candle and returns current values.
        """
        self.candle_count += 1
        high = high if high is not None else close
        low = low if low is not None else close

        self.close_queue.append(close)

        # 1. Rolling VWAP
        pv = close * volume
        if len(self.vwap_queue) == self.vwap_queue.maxlen:
            old_pv, old_vol = self.vwap_queue[0]
            self.vwap_pv_sum -= old_pv
            self.vwap_vol_sum -= old_vol

        self.vwap_queue.append((pv, volume))
        self.vwap_pv_sum += pv
        self.vwap_vol_sum += volume
        vwap = self.vwap_pv_sum / self.vwap_vol_sum if self.vwap_vol_sum > 0 else close

        # 2. VWAP Standard Deviation & Statistical Bands
        if len(self.close_queue) >= 20:
            variance = sum((c - vwap) ** 2 for c in self.close_queue) / len(self.close_queue)
            vwap_std = math.sqrt(variance)
            upper_band = vwap + (self.band_multiplier * vwap_std)
            lower_band = vwap - (self.band_multiplier * vwap_std)
        else:
            vwap_std = None
            upper_band = None
            lower_band = None

        # 3. RSI Calculation
        rsi: Optional[float] = None
        if self.prev_close is not None:
            change = close - self.prev_close
            self.rsi_queue.append(change)
            if len(self.rsi_queue) == self.rsi_period:
                gains = sum(x for x in self.rsi_queue if x > 0)
                losses = abs(sum(x for x in self.rsi_queue if x < 0))
                if losses == 0:
                    rsi = 100.0
                elif gains == 0:
                    rsi = 0.0
                else:
                    rsi = 100.0 - (100.0 / (1.0 + (gains / losses)))

        # 4. ADX & ATR Calculation
        adx: Optional[float] = None
        atr: Optional[float] = None
        if self.prev_close is not None and self.prev_high is not None and self.prev_low is not None:
            tr = max(
                high - low,
                abs(high - self.prev_close),
                abs(low - self.prev_close)
            )
            self.tr_queue.append(tr)
            atr = sum(self.tr_queue) / len(self.tr_queue)

            up_move = high - self.prev_high
            down_move = self.prev_low - low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

            self.plus_dm_queue.append(plus_dm)
            self.minus_dm_queue.append(minus_dm)

            if len(self.tr_queue) == self.adx_period:
                tr_sum = sum(self.tr_queue) + 1e-9
                plus_di = 100.0 * (sum(self.plus_dm_queue) / tr_sum)
                minus_di = 100.0 * (sum(self.minus_dm_queue) / tr_sum)
                di_sum = plus_di + minus_di + 1e-9
                dx = 100.0 * abs(plus_di - minus_di) / di_sum
                adx = dx

        # 5. Incremental 200 EMA
        if self.ema is None:
            self.ema = close
        else:
            self.ema += self.alpha_ema * (close - self.ema)

        self.prev_close = close
        self.prev_high = high
        self.prev_low = low

        return IndicatorValues(
            vwap=vwap,
            vwap_std=vwap_std,
            upper_band=upper_band,
            lower_band=lower_band,
            rsi=rsi,
            adx=adx,
            atr=atr,
            ema=self.ema
        )

    def warmup(self, klines: List[List]) -> IndicatorValues:
        """
        Feeds historical klines to warm up indicators.
        Klines format follows Binance Futures format:
        [open_time, open, high, low, close, volume, ...]
        """
        last_val = IndicatorValues(
            vwap=None, vwap_std=None, upper_band=None, lower_band=None,
            rsi=None, adx=None, atr=None, ema=None
        )
        for k in klines:
            open_p = float(k[1])
            high = float(k[2])
            low = float(k[3])
            close = float(k[4])
            volume = float(k[5])
            last_val = self.update(
                close=close,
                volume=volume,
                high=high,
                low=low,
                open_price=open_p
            )
        return last_val
