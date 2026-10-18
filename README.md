# ProBot — Binance USDT-M Futures Trading System

Automated algorithmic trading bot for Binance Perpetual USDT-M Futures using a Mean Reversion strategy (EMA Trend Filter + VWAP Fair Value Pullback + RSI Momentum Exhaustion Filter) with automated conditional Take-Profit (TP) and Stop-Loss (SL) algo orders.

---

## Architecture & Directory Structure

```
test-env/
├── bot.py                  # Streamlined main runner and CLI entrypoint (Live / Production)
├── testbot.py              # Strategy testing, testnet API execution & dataset generator
├── data/                   # Generated candle records, trades CSV, and performance metrics
│   ├── test_candles_*.csv  # Per-symbol candle snapshots (EMA, RSI, VWAP, signals, bias)
│   ├── test_trades.csv     # Executed trade log (entry, exit, PnL, fees, duration, mode)
│   └── test_metrics.json   # Win rate, profit factor, drawdown & cumulative PnL
├── modules/                # Shared modular architecture
│   ├── __init__.py         # Public exports
│   ├── config.py           # Configuration dataclass, environment loader & UTF-8 logging
│   ├── indicators.py       # O(1) Incremental technical indicators (EMA, RSI, VWAP, ATR)
│   ├── exchange.py         # Binance Futures REST client, cached filters, and signed Algo Orders
│   ├── strategy.py         # Trading rules, signal evaluation, Decimal TP/SL & sizing
│   ├── engine.py           # Production single-symbol ProBot engine, WebSocket & watchdog
│   └── recorder.py         # Dataset recorder (CSV/JSON) and strategy performance auditor
├── tests/
│   └── test_modules.py     # 8 unit tests for indicators, strategy math, and data recorder
├── backup/                 # Archived previous versions & backups
├── .env                    # Binance API credentials and trading mode
└── ProBot_Documentation.docx # Technical documentation
```

---

## Strategy Summary

ProBot trades 5-minute candles on Binance USDT-M Futures:

| Parameter | Value | Description |
|---|---|---|
| **Interval** | 5m | Closed 5-minute candles |
| **Leverage** | 3× | Configurable per symbol |
| **Trend Filter** | 198 EMA | Price above EMA = Long bias; Price below EMA = Short bias |
| **Fair Value** | 288 VWAP | 24-hour rolling volume-weighted average price |
| **Proximity Band**| 0.2% | Entry price must be within 0.2% of VWAP |
| **Momentum Filter**| 15 RSI | Long requires RSI < 42; Short requires RSI > 60 |
| **Take Profit** | +0.8% | Placed via `/fapi/v1/algoOrder` (`TAKE_PROFIT_MARKET`) |
| **Stop Loss** | -0.5% | Placed via `/fapi/v1/algoOrder` (`STOP_MARKET`) |
| **Risk/Reward** | 1:1.6 | Asymmetric positive risk-to-reward ratio |

---

## Setup & Quickstart

### 1. Environment Configuration (`.env`)

Configure your Binance API keys in `.env`:

```ini
TRADING_MODE=live
BINANCE_LIVE_API_KEY=your_live_api_key_here
BINANCE_LIVE_API_SECRET=your_live_api_secret_here

# Testnet credentials
BINANCE_TEST_API_KEY=your_testnet_api_key_here
BINANCE_TEST_API_SECRET=your_testnet_api_secret_here
```

---

### 2. Testing the Strategy with `testbot.py`

`testbot.py` is designed to test the strategy on Binance Testnet or real-time paper simulation, and automatically generates datasets for future machine learning and restructuring.

#### A. Instant Historical Simulation (Fast Backtest)
Download the last N candles from Binance, test the strategy candle-by-candle, and generate trade datasets in seconds:
```bash
python testbot.py --backtest 500
```

#### B. Live Streaming Test (Real-Time WebSocket)
Run live on Binance Futures Testnet (or paper mode):
```bash
# Auto-detects testnet API credentials with automatic paper fallback
python testbot.py

# Force paper-trading simulation mode on live market data
python testbot.py --mode paper
```

#### C. Datasets Generated
All datasets are stored in `data/`:
- `data/test_candles_ethusdt.csv`: Every closed candle with OHLCV, 198 EMA, 15 RSI, 288 VWAP, VWAP delta %, trend bias, and signal evaluated.
- `data/test_trades.csv`: Full trade lifecycle (entry price, exit price, TP/SL levels, PnL, fees, and balance).
- `data/test_metrics.json`: Win rate, profit factor, total fees, and return on capital.

---

### 3. Production Trading with `bot.py`

Run production bot for ETHUSDT:
```bash
# Default (ETHUSDT)
python bot.py

# Testnet mode
python bot.py --mode testnet
```

---

### 4. Run Unit Tests

Execute the automated test suite without hitting Binance network endpoints:
```bash
python -m unittest tests/test_modules.py
```
