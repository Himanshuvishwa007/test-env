"""
Data recorder and performance tracking module for ProBot.
Logs candle-by-candle indicators, signals, and trade execution data to CSV and JSON
for post-trade analysis, machine learning, and strategy restructuring.
"""

import os
import csv
import json
import logging
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

log = logging.getLogger("ProBot")


@dataclass
class CandleRecord:
    timestamp: int
    datetime_str: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema: Optional[float]
    rsi: Optional[float]
    vwap: Optional[float]
    vwap_delta_pct: Optional[float]
    trend_bias: str
    vwap_status: str
    rsi_status: str
    signal: str
    action_taken: str
    in_trade: bool
    cooldown: int


@dataclass
class TradeRecord:
    trade_id: int
    timestamp: int
    datetime_str: str
    symbol: str
    side: str
    entry_price: float
    quantity: float
    notional_usdt: float
    leverage: int
    margin_used: float
    tp_price: float
    sl_price: float
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: str = "PENDING"
    duration_candles: int = 0
    gross_pnl: float = 0.0
    fee: float = 0.0
    net_pnl: float = 0.0
    pnl_pct: float = 0.0
    wallet_balance: float = 0.0
    execution_mode: str = "TESTNET_API"


class DataRecorder:
    """
    Records trade analytics, signals, and market state snapshots to CSV/JSON.
    """

    def __init__(self, output_dir: str = "data"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        self.trades_file = os.path.join(self.output_dir, "test_trades.csv")
        self.metrics_file = os.path.join(self.output_dir, "test_metrics.json")
        self.candle_files: Dict[str, str] = {}

        # Trade counting and state
        self._trade_counter = 0
        self.active_trades: Dict[str, TradeRecord] = {}
        self.closed_trades: List[TradeRecord] = []

        # Metrics accumulator
        self.metrics = {
            "total_candles": 0,
            "total_signals_buy": 0,
            "total_signals_sell": 0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "gross_profit_usdt": 0.0,
            "gross_loss_usdt": 0.0,
            "total_fees_usdt": 0.0,
            "net_pnl_usdt": 0.0,
            "profit_factor": 0.0,
            "initial_balance": 1000.0,
            "current_balance": 1000.0,
            "return_on_capital_pct": 0.0,
            "updated_at": datetime.utcnow().isoformat()
        }

        self._init_trades_csv()

    def _init_trades_csv(self) -> None:
        """Initializes trades CSV header if not already present."""
        if not os.path.exists(self.trades_file) or os.path.getsize(self.trades_file) == 0:
            headers = [
                "trade_id", "timestamp", "datetime_str", "symbol", "side",
                "entry_price", "quantity", "notional_usdt", "leverage", "margin_used",
                "tp_price", "sl_price", "exit_time", "exit_price", "exit_reason",
                "duration_candles", "gross_pnl", "fee", "net_pnl", "pnl_pct",
                "wallet_balance", "execution_mode"
            ]
            with open(self.trades_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)

    def _get_candle_file(self, symbol: str) -> str:
        """Returns or creates the CSV path for a symbol's candle data."""
        if symbol not in self.candle_files:
            filepath = os.path.join(self.output_dir, f"test_candles_{symbol.lower()}.csv")
            self.candle_files[symbol] = filepath
            if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                headers = [
                    "timestamp", "datetime_str", "symbol", "open", "high", "low", "close", "volume",
                    "ema", "rsi", "vwap", "vwap_delta_pct", "trend_bias", "vwap_status", "rsi_status",
                    "signal", "action_taken", "in_trade", "cooldown"
                ]
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
        return self.candle_files[symbol]

    def record_candle(self, record: CandleRecord) -> None:
        """Appends a candle record to the symbol-specific CSV."""
        filepath = self._get_candle_file(record.symbol)
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                record.timestamp,
                record.datetime_str,
                record.symbol,
                f"{record.open:.4f}",
                f"{record.high:.4f}",
                f"{record.low:.4f}",
                f"{record.close:.4f}",
                f"{record.volume:.4f}",
                f"{record.ema:.4f}" if record.ema is not None else "",
                f"{record.rsi:.2f}" if record.rsi is not None else "",
                f"{record.vwap:.4f}" if record.vwap is not None else "",
                f"{record.vwap_delta_pct:.3f}" if record.vwap_delta_pct is not None else "",
                record.trend_bias,
                record.vwap_status,
                record.rsi_status,
                record.signal,
                record.action_taken,
                record.in_trade,
                record.cooldown
            ])

        self.metrics["total_candles"] += 1
        if record.signal == "BUY":
            self.metrics["total_signals_buy"] += 1
        elif record.signal == "SELL":
            self.metrics["total_signals_sell"] += 1

    def record_trade_open(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        leverage: int,
        tp_price: float,
        sl_price: float,
        balance: float,
        mode: str = "TESTNET_API"
    ) -> TradeRecord:
        """Records a new trade entry."""
        self._trade_counter += 1
        now = datetime.utcnow()
        notional = entry_price * quantity
        margin = notional / leverage if leverage > 0 else notional

        trade = TradeRecord(
            trade_id=self._trade_counter,
            timestamp=int(now.timestamp() * 1000),
            datetime_str=now.strftime("%Y-%m-%d %H:%M:%S"),
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            notional_usdt=round(notional, 2),
            leverage=leverage,
            margin_used=round(margin, 2),
            tp_price=tp_price,
            sl_price=sl_price,
            wallet_balance=round(balance, 2),
            execution_mode=mode
        )
        self.active_trades[symbol] = trade
        return trade

    def record_trade_close(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        current_balance: float,
        duration_candles: int = 1
    ) -> Optional[TradeRecord]:
        """Calculates PnL, appends closed trade to CSV, and updates metrics."""
        trade = self.active_trades.pop(symbol, None)
        if not trade:
            return None

        now = datetime.utcnow()
        trade.exit_time = now.strftime("%Y-%m-%d %H:%M:%S")
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason
        trade.duration_candles = duration_candles

        # Calculate PnL: Long: (exit - entry)*qty, Short: (entry - exit)*qty
        if trade.side == "BUY":
            gross = (exit_price - trade.entry_price) * trade.quantity
        else:
            gross = (trade.entry_price - exit_price) * trade.quantity

        # Fee: ~0.04% taker fee on both entry and exit notional
        entry_notional = trade.entry_price * trade.quantity
        exit_notional = exit_price * trade.quantity
        fees = (entry_notional + exit_notional) * 0.0004

        net = gross - fees
        pnl_pct = (net / trade.margin_used * 100.0) if trade.margin_used > 0 else 0.0

        trade.gross_pnl = round(gross, 4)
        trade.fee = round(fees, 4)
        trade.net_pnl = round(net, 4)
        trade.pnl_pct = round(pnl_pct, 2)
        trade.wallet_balance = round(current_balance, 2)

        # Write to trades CSV
        with open(self.trades_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                trade.trade_id, trade.timestamp, trade.datetime_str, trade.symbol, trade.side,
                trade.entry_price, trade.quantity, trade.notional_usdt, trade.leverage, trade.margin_used,
                trade.tp_price, trade.sl_price, trade.exit_time, trade.exit_price, trade.exit_reason,
                trade.duration_candles, trade.gross_pnl, trade.fee, trade.net_pnl, trade.pnl_pct,
                trade.wallet_balance, trade.execution_mode
            ])

        self.closed_trades.append(trade)
        self._update_metrics(trade, current_balance)
        return trade

    def _update_metrics(self, trade: TradeRecord, current_balance: float) -> None:
        """Updates cumulative performance metrics."""
        self.metrics["total_trades"] += 1
        if trade.net_pnl > 0:
            self.metrics["winning_trades"] += 1
            self.metrics["gross_profit_usdt"] += trade.gross_pnl
        else:
            self.metrics["losing_trades"] += 1
            self.metrics["gross_loss_usdt"] += abs(trade.gross_pnl)

        self.metrics["total_fees_usdt"] += trade.fee
        self.metrics["net_pnl_usdt"] += trade.net_pnl

        wins = self.metrics["winning_trades"]
        total = self.metrics["total_trades"]
        self.metrics["win_rate_pct"] = round((wins / total * 100.0) if total > 0 else 0.0, 2)

        gross_loss = self.metrics["gross_loss_usdt"]
        self.metrics["profit_factor"] = round(
            (self.metrics["gross_profit_usdt"] / gross_loss) if gross_loss > 0 else 999.0, 2
        )
        self.metrics["current_balance"] = round(current_balance, 2)
        init_bal = self.metrics["initial_balance"]
        self.metrics["return_on_capital_pct"] = round(
            ((current_balance - init_bal) / init_bal * 100.0) if init_bal > 0 else 0.0, 2
        )
        self.metrics["updated_at"] = datetime.utcnow().isoformat()

        # Save metrics JSON
        try:
            with open(self.metrics_file, "w", encoding="utf-8") as f:
                json.dump(self.metrics, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write metrics file: {e}")

    def get_summary_report(self) -> str:
        """Returns a formatted performance text summary."""
        m = self.metrics
        return (
            f"\n{'='*55}\n"
            f"          ProBot Strategy Performance Report          \n"
            f"{'='*55}\n"
            f" Total Candles Evaluated : {m['total_candles']}\n"
            f" Signals (Long / Short)  : {m['total_signals_buy']} / {m['total_signals_sell']}\n"
            f" Total Executed Trades   : {m['total_trades']}\n"
            f" Win / Loss              : {m['winning_trades']} / {m['losing_trades']} ({m['win_rate_pct']}% Win Rate)\n"
            f" Profit Factor           : {m['profit_factor']}\n"
            f" Net PnL                 : {m['net_pnl_usdt']:+.2f} USDT\n"
            f" Total Fees Paid         : {m['total_fees_usdt']:.2f} USDT\n"
            f" Current Balance         : {m['current_balance']:.2f} USDT ({m['return_on_capital_pct']:+.2f}%)\n"
            f" Saved Data Files        : {self.output_dir}/\n"
            f"{'='*55}"
        )
