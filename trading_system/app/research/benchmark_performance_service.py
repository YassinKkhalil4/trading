from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select

from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


BENCHMARK_PERFORMANCE_VERSION = "benchmark_performance_v1"
SUPPORTED_BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "CASH")
CASH_BENCHMARK = "CASH"


@dataclass(frozen=True)
class BenchmarkComparison:
    benchmark: str
    available: bool
    missing_data_reason: str | None
    portfolio_return: float | None
    benchmark_return: float | None
    alpha: float | None
    max_drawdown: float | None
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    trade_count: int
    period_start: datetime | None
    period_end: datetime | None
    version: str = BENCHMARK_PERFORMANCE_VERSION


@dataclass(frozen=True)
class BenchmarkPerformanceResult:
    success: bool
    reason: str
    comparisons: dict[str, BenchmarkComparison]
    version: str = BENCHMARK_PERFORMANCE_VERSION


class BenchmarkPerformanceService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = BenchmarkJournalRepository(repository)

    def compare(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        benchmarks: tuple[str, ...] = SUPPORTED_BENCHMARKS,
        provider: str | None = None,
        timeframe: str = "1Min",
        journal_entries: list[models.TradeJournal] | None = None,
    ) -> BenchmarkPerformanceResult:
        entries = journal_entries or self.repository.closed_journal_entries(start, end)
        closed = _closed_trades(entries)
        if not closed:
            reason = "No closed journal trades were available for benchmark comparison."
            comparisons = {
                benchmark.upper(): _empty_comparison(
                    benchmark=benchmark.upper(),
                    missing_data_reason=reason,
                )
                for benchmark in benchmarks
            }
            return BenchmarkPerformanceResult(False, reason, comparisons)

        period_start, period_end = _trade_period(closed, start, end)
        portfolio = _portfolio_metrics(closed)
        comparisons: dict[str, BenchmarkComparison] = {}

        for benchmark in benchmarks:
            symbol = benchmark.upper()
            if symbol == CASH_BENCHMARK:
                comparisons[symbol] = _build_comparison(
                    benchmark=symbol,
                    available=True,
                    missing_data_reason=None,
                    portfolio=portfolio,
                    benchmark_return=0.0,
                    period_start=period_start,
                    period_end=period_end,
                )
                continue

            frame, missing_reason = self.repository.benchmark_candles(
                symbol,
                period_start=period_start,
                period_end=period_end,
                provider=provider,
                timeframe=timeframe,
            )
            if missing_reason is not None:
                comparisons[symbol] = _empty_comparison(
                    benchmark=symbol,
                    missing_data_reason=missing_reason,
                    trade_count=portfolio["trade_count"],
                    period_start=period_start,
                    period_end=period_end,
                )
                continue

            benchmark_return = _price_return(frame)
            if benchmark_return is None:
                comparisons[symbol] = _empty_comparison(
                    benchmark=symbol,
                    missing_data_reason=(
                        f"No {symbol} clean candles cover the journal period "
                        f"{period_start.isoformat()} to {period_end.isoformat()}."
                    ),
                    trade_count=portfolio["trade_count"],
                    period_start=period_start,
                    period_end=period_end,
                )
                continue

            comparisons[symbol] = _build_comparison(
                benchmark=symbol,
                available=True,
                missing_data_reason=None,
                portfolio=portfolio,
                benchmark_return=benchmark_return,
                period_start=period_start,
                period_end=period_end,
            )

        available_count = sum(1 for item in comparisons.values() if item.available)
        if available_count == 0:
            return BenchmarkPerformanceResult(
                False,
                "Benchmark comparison failed because no benchmark data was available.",
                comparisons,
            )
        return BenchmarkPerformanceResult(
            True,
            f"Benchmark comparison completed for {available_count} benchmark(s).",
            comparisons,
        )


class BenchmarkJournalRepository:
    __slots__ = ("_clean_candles_df", "_session")

    def __init__(self, repository: TradingRepository) -> None:
        self._session = repository.session
        self._clean_candles_df = repository.clean_candles_df

    def closed_journal_entries(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> list[models.TradeJournal]:
        stmt = select(models.TradeJournal).where(models.TradeJournal.pnl.is_not(None))
        if start is not None:
            stmt = stmt.where(models.TradeJournal.created_at >= start)
        if end is not None:
            stmt = stmt.where(models.TradeJournal.created_at <= end)
        return list(self._session.scalars(stmt.order_by(models.TradeJournal.created_at)).all())

    def benchmark_candles(
        self,
        symbol: str,
        *,
        period_start: datetime,
        period_end: datetime,
        provider: str | None,
        timeframe: str,
    ) -> tuple[pd.DataFrame, str | None]:
        providers = [provider] if provider else ["alpaca_market_data", "yahoo_chart"]
        for candidate in providers:
            if candidate is None:
                continue
            frame = self._clean_candles_df(
                symbol,
                provider=candidate,
                timeframe=timeframe,
                limit=5000,
                valid_only=True,
            )
            if frame.empty:
                continue
            frame = _ensure_utc_index(frame)
            filtered = _filter_period(frame, period_start, period_end)
            if len(filtered) >= 2:
                return filtered, None
        return pd.DataFrame(), (
            f"No clean {symbol.upper()} candles were found for provider(s) "
            f"{', '.join(item for item in providers if item)}."
        )


def _closed_trades(entries: list[models.TradeJournal]) -> list[models.TradeJournal]:
    return [entry for entry in entries if entry.pnl is not None]


def _trade_period(
    closed: list[models.TradeJournal],
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    timestamps = [_entry_timestamp(entry) for entry in closed]
    period_start = start or min(timestamps)
    period_end = end or max(timestamps)
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=UTC)
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return period_start, period_end


def _entry_timestamp(entry: models.TradeJournal) -> datetime:
    timestamp = entry.source_timestamp or entry.created_at
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp


def _portfolio_metrics(closed: list[models.TradeJournal]) -> dict[str, Any]:
    pnls = [float(entry.pnl) for entry in closed if entry.pnl is not None]
    notionals = [
        float(entry.actual_entry)
        for entry in closed
        if entry.actual_entry is not None and entry.actual_entry > 0
    ]
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))

    portfolio_return = None
    if notionals:
        portfolio_return = sum(pnls) / sum(notionals)

    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")

    win_rate = (len(winners) / len(pnls)) if pnls else None
    expectancy = (sum(pnls) / len(pnls)) if pnls else None
    max_drawdown = _max_drawdown(closed, notionals, pnls)

    return {
        "trade_count": len(closed),
        "portfolio_return": portfolio_return,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
    }


def _max_drawdown(
    closed: list[models.TradeJournal],
    notionals: list[float],
    pnls: list[float],
) -> float | None:
    if not pnls:
        return None
    ordered = sorted(closed, key=_entry_timestamp)
    cumulative = 0.0
    equity_curve: list[float] = []
    base_capital = sum(notionals) if notionals else float(len(pnls))
    if base_capital <= 0:
        base_capital = float(len(pnls))
    for entry in ordered:
        cumulative += float(entry.pnl or 0.0)
        equity_curve.append(base_capital + cumulative)

    peak = equity_curve[0]
    worst = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak)
    return worst if equity_curve else None


def _ensure_utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    if frame.index.tz is None:
        return frame.set_axis(frame.index.tz_localize(UTC))
    return frame.set_axis(frame.index.tz_convert(UTC))


def _filter_period(
    frame: pd.DataFrame,
    period_start: datetime,
    period_end: datetime,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    start = pd.Timestamp(period_start)
    end = pd.Timestamp(period_end)
    if start.tzinfo is None:
        start = start.tz_localize(UTC)
    if end.tzinfo is None:
        end = end.tz_localize(UTC)
    filtered = frame[(frame.index >= start) & (frame.index <= end)]
    if len(filtered) >= 2:
        return filtered
    forward = frame[frame.index >= start]
    if len(forward) >= 2:
        return forward
    before_start = frame[frame.index <= start]
    after_end = frame[frame.index >= end]
    if before_start.empty or after_end.empty:
        return pd.DataFrame()
    return pd.concat([before_start.tail(1), after_end.head(1)]).sort_index()


def _price_return(frame: pd.DataFrame) -> float | None:
    if len(frame) < 2:
        return None
    start_close = float(frame["close"].iloc[0])
    end_close = float(frame["close"].iloc[-1])
    if start_close <= 0:
        return None
    return (end_close / start_close) - 1.0


def _build_comparison(
    *,
    benchmark: str,
    available: bool,
    missing_data_reason: str | None,
    portfolio: dict[str, Any],
    benchmark_return: float,
    period_start: datetime,
    period_end: datetime,
) -> BenchmarkComparison:
    portfolio_return = portfolio["portfolio_return"]
    alpha = None
    if portfolio_return is not None:
        alpha = portfolio_return - benchmark_return
    return BenchmarkComparison(
        benchmark=benchmark,
        available=available,
        missing_data_reason=missing_data_reason,
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        alpha=alpha,
        max_drawdown=portfolio["max_drawdown"],
        win_rate=portfolio["win_rate"],
        profit_factor=portfolio["profit_factor"],
        expectancy=portfolio["expectancy"],
        trade_count=portfolio["trade_count"],
        period_start=period_start,
        period_end=period_end,
    )


def _empty_comparison(
    *,
    benchmark: str,
    missing_data_reason: str,
    trade_count: int = 0,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> BenchmarkComparison:
    return BenchmarkComparison(
        benchmark=benchmark,
        available=False,
        missing_data_reason=missing_data_reason,
        portfolio_return=None,
        benchmark_return=None,
        alpha=None,
        max_drawdown=None,
        win_rate=None,
        profit_factor=None,
        expectancy=None,
        trade_count=trade_count,
        period_start=period_start,
        period_end=period_end,
    )
