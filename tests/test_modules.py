"""
Unit tests for ProBot modular components.
Verifies config, indicators math, and strategy signal logic without network calls.
"""

import unittest
from modules.config import BotConfig
from modules.indicators import IndicatorTracker, IndicatorValues
from modules.strategy import MeanReversionStrategy, TradeTargets


class TestProBotModules(unittest.TestCase):

    def setUp(self):
        self.config = BotConfig(
            symbols=["ETHUSDT"],
            leverage=3,
            vwap_period=20,
            vwap_band_mult=1.5,
            rsi_period=14,
            rsi_oversold=38.0,
            rsi_overbought=62.0,
            adx_period=14,
            adx_max=28.0,
            sl_pct=0.010,
            wick_ratio=0.5
        )
        self.strategy = MeanReversionStrategy(self.config)

    def test_indicators_vwap_bands_rsi_adx(self):
        tracker = IndicatorTracker(
            vwap_period=20,
            band_multiplier=1.5,
            rsi_period=5,
            adx_period=5,
            atr_period=5
        )

        # Feed 25 sequential candles
        prices = [100.0 + (i % 5) for i in range(25)]
        volumes = [10.0 + i for i in range(25)]

        last_vals = None
        for p, v in zip(prices, volumes):
            last_vals = tracker.update(
                close=p,
                volume=v,
                high=p + 2.0,
                low=p - 2.0,
                open_price=p - 0.5
            )

        self.assertIsNotNone(last_vals.vwap)
        self.assertIsNotNone(last_vals.upper_band)
        self.assertIsNotNone(last_vals.lower_band)
        self.assertIsNotNone(last_vals.rsi)
        self.assertIsNotNone(last_vals.adx)
        self.assertTrue(last_vals.is_complete)
        self.assertGreater(last_vals.upper_band, last_vals.lower_band)

    def test_strategy_long_signal(self):
        # Long Breakout: close > upper_band, close > open, rsi >= 50, close > vwap
        indicators = IndicatorValues(
            vwap=2000.0,
            vwap_std=20.0,
            upper_band=2030.0,
            lower_band=1970.0,
            rsi=58.0,
            adx=20.0,
            atr=10.0,
            ema=1990.0
        )
        # Candle: open=2025, high=2040, low=2022, close=2035 (breaks upper_band=2030, green candle)
        signal = self.strategy.evaluate_signal(
            close=2035.0,
            indicators=indicators,
            open_price=2025.0,
            high=2040.0,
            low=2022.0
        )
        self.assertEqual(signal, "BUY")

    def test_strategy_short_signal(self):
        # Short Breakdown: close < lower_band, close < open, rsi <= 50, close < vwap
        indicators = IndicatorValues(
            vwap=2000.0,
            vwap_std=20.0,
            upper_band=2030.0,
            lower_band=1970.0,
            rsi=42.0,
            adx=20.0,
            atr=10.0,
            ema=2010.0
        )
        # Candle: open=1975, high=1978, low=1960, close=1965 (breaks lower_band=1970, red candle)
        signal = self.strategy.evaluate_signal(
            close=1965.0,
            indicators=indicators,
            open_price=1975.0,
            high=1978.0,
            low=1960.0
        )
        self.assertEqual(signal, "SELL")

    def test_strategy_adx_runaway_protection(self):
        # When ADX is extreme (>= adx_max), signals are blocked
        indicators = IndicatorValues(
            vwap=2000.0,
            vwap_std=20.0,
            upper_band=2030.0,
            lower_band=1970.0,
            rsi=58.0,
            adx=35.0,  # Violent moves exceeding adx_max (28.0)
            atr=15.0,
            ema=1990.0
        )
        signal = self.strategy.evaluate_signal(
            close=2035.0,
            indicators=indicators,
            open_price=2025.0,
            high=2040.0,
            low=2022.0
        )
        self.assertIsNone(signal)

    def test_calculate_targets(self):
        # Long targets with VWAP reversion target
        targets_buy = self.strategy.calculate_targets(
            side="BUY",
            entry_price=1960.0,
            tick_size=0.1,
            vwap_target=2000.0
        )
        self.assertEqual(targets_buy.entry_price, 1960.0)
        # TP should target VWAP (2000.0)
        self.assertEqual(targets_buy.take_profit, 2000.0)
        # SL = -1.0% = 1940.4
        self.assertAlmostEqual(targets_buy.stop_loss, 1940.4, places=1)

    def test_calculate_quantity(self):
        qty = self.strategy.calculate_quantity(
            balance=100.0,
            price=2000.0,
            step_size=0.001,
            min_notional=5.0
        )
        self.assertEqual(qty, 0.135)

    def test_exchange_signing_and_rounding(self):
        from modules.exchange import BinanceExchange
        exchange = BinanceExchange(api_key="test_key", api_secret="test_secret", testnet=True)
        exchange._symbol_filters["ETHUSDT"] = {"step": 0.001, "tick": 0.1, "min_notional": 5.0}

        self.assertEqual(exchange.round_qty("ETHUSDT", 1.23456), 1.234)
        self.assertEqual(exchange.round_price("ETHUSDT", 2150.389), 2150.3)

        params = {"symbol": "ETHUSDT", "side": "BUY"}
        signed = exchange._sign_request(params)
        self.assertIn("timestamp", signed)
        self.assertIn("signature", signed)
        self.assertEqual(len(signed["signature"]), 64)

    def test_data_recorder(self):
        import shutil
        import tempfile
        from modules.recorder import DataRecorder, CandleRecord

        temp_dir = tempfile.mkdtemp()
        try:
            recorder = DataRecorder(output_dir=temp_dir)
            rec = CandleRecord(
                timestamp=1600000000000,
                datetime_str="2026-10-14 12:00:00",
                symbol="ETHUSDT",
                open=2000.0,
                high=2010.0,
                low=1990.0,
                close=2005.0,
                volume=100.0,
                ema=2000.0,
                rsi=40.0,
                vwap=2002.0,
                vwap_delta_pct=0.15,
                trend_bias="BULLISH",
                vwap_status="WITHIN_BAND",
                rsi_status="OVERSOLD",
                signal="BUY",
                action_taken="ORDER_PLACED",
                in_trade=False,
                cooldown=0
            )
            recorder.record_candle(rec)
            self.assertEqual(recorder.metrics["total_candles"], 1)
            self.assertEqual(recorder.metrics["total_signals_buy"], 1)

            recorder.record_trade_open(
                symbol="ETHUSDT",
                side="BUY",
                entry_price=2005.0,
                quantity=1.0,
                leverage=3,
                tp_price=2021.0,
                sl_price=1995.0,
                balance=1000.0
            )
            closed_trade = recorder.record_trade_close(
                symbol="ETHUSDT",
                exit_price=2021.0,
                exit_reason="TAKE_PROFIT",
                current_balance=1015.0
            )
            self.assertIsNotNone(closed_trade)
            self.assertEqual(recorder.metrics["total_trades"], 1)
            self.assertEqual(recorder.metrics["winning_trades"], 1)
            self.assertEqual(recorder.metrics["win_rate_pct"], 100.0)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
