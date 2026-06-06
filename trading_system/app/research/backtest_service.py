from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.db.repositories import TradingRepository
from trading_system.app.research.vectorbt_backtests import (
    BacktestAssumptions,
    run_vwap_reclaim_backtest,
)


BACKTEST_SERVICE_VERSION = "backtest_service_v1"


@dataclass(frozen=True)
class BacktestRunResult:
    success: bool
    report_id: str | None
    strategy_id: str
    symbols_seen: int
    reason: str
    version: str = BACKTEST_SERVICE_VERSION


class BacktestService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_vwap_reclaim(
        self,
        *,
        symbols: list[str] | None = None,
        provider: str = "alpaca_market_data",
        timeframe: str = "1Min",
        universe_name: str = "active_symbols",
        assumptions: BacktestAssumptions | None = None,
    ) -> BacktestRunResult:
        symbols = [item.upper() for item in (symbols or self.repository.active_symbols())]
        if not symbols:
            return BacktestRunResult(False, None, "VWAP_RECLAIM", 0, "No symbols supplied.")
        assumptions = assumptions or BacktestAssumptions()
        metrics_by_symbol = {}
        reports = []
        for symbol in symbols:
            frame = self.repository.clean_candles_df(
                symbol,
                timeframe=timeframe,
                provider=provider,
                limit=2000,
            )
            if frame.empty:
                continue
            relative_volume = frame["volume"] / frame["volume"].rolling(20).mean().replace(0, 1)
            result = run_vwap_reclaim_backtest(
                frame,
                relative_volume=relative_volume.fillna(0),
                assumptions=assumptions,
            )
            stats = result["stats"]
            metrics_by_symbol[symbol] = stats
            reports.append(result)
        if not reports:
            return BacktestRunResult(
                False,
                None,
                "VWAP_RECLAIM",
                len(symbols),
                "No persisted candles were available for backtesting.",
            )
        metrics = {
            "symbols": list(metrics_by_symbol),
            "metrics_by_symbol": metrics_by_symbol,
            "trade_count": _sum_metric(metrics_by_symbol, ["Total Trades", "total_trades", "trade_count"]),
            "profit_factor": _avg_metric(metrics_by_symbol, ["Profit Factor", "profit_factor"]),
            "max_drawdown": _avg_metric(metrics_by_symbol, ["Max Drawdown [%]", "max_drawdown"]),
            "expectancy": _avg_metric(metrics_by_symbol, ["Expectancy", "expectancy"]),
        }
        row = self.repository.store_backtest_report(
            strategy_id="VWAP_RECLAIM",
            strategy_version="v1",
            universe_name=universe_name,
            assumptions=assumptions.__dict__,
            metrics=metrics,
            report_uri=None,
            survivorship_bias_warning=assumptions.survivorship_bias_warning,
            reason="VWAP Reclaim VectorBT backtest stored with slippage, commission, spread, and entry-delay assumptions.",
        )
        return BacktestRunResult(
            True,
            row.id,
            "VWAP_RECLAIM",
            len(symbols),
            "Backtest report stored.",
        )


def _sum_metric(metrics_by_symbol: dict, names: list[str]) -> float:
    total = 0.0
    for metrics in metrics_by_symbol.values():
        for name in names:
            value = metrics.get(name)
            if value is not None:
                try:
                    total += float(value)
                except (TypeError, ValueError):
                    pass
                break
    return total


def _avg_metric(metrics_by_symbol: dict, names: list[str]) -> float | None:
    values = []
    for metrics in metrics_by_symbol.values():
        for name in names:
            value = metrics.get(name)
            if value is not None:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    pass
                break
    return sum(values) / len(values) if values else None
