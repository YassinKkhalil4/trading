from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.research.benchmark_performance_service import BenchmarkPerformanceService


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _insert_benchmark_candles(
    repo: TradingRepository,
    *,
    symbol: str,
    start: datetime,
    start_price: float,
    end_price: float,
    count: int = 5,
    provider: str = "alpaca_market_data",
) -> None:
    for idx in range(count):
        ts = start + timedelta(minutes=idx)
        if idx == count - 1:
            price = end_price
        else:
            step = (end_price - start_price) / max(count - 1, 1)
            price = start_price + step * idx
        raw_id = repo.store_raw_candle(
            {
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": "test",
            }
        )


def test_calculates_alpha_vs_spy():
    repo = _repo()
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="Alpha test trade.",
        actual_entry=100.0,
        actual_exit=150.0,
        pnl=50.0,
        human_notes=None,
        mistake_tags=[],
        change_reason="Benchmark alpha test.",
    )
    journal = repo.session.scalar(
        select(models.TradeJournal).order_by(models.TradeJournal.created_at.desc()).limit(1)
    )
    assert journal is not None
    start = journal.source_timestamp
    _insert_benchmark_candles(
        repo,
        symbol="SPY",
        start=start,
        start_price=100.0,
        end_price=110.0,
        count=5,
    )

    result = BenchmarkPerformanceService(repo).compare(
        benchmarks=("SPY",),
        journal_entries=[journal],
    )
    spy = result.comparisons["SPY"]

    assert result.success is True
    assert spy.available is True
    assert spy.missing_data_reason is None
    assert spy.benchmark_return == pytest.approx(0.1)
    assert spy.portfolio_return == pytest.approx(0.5)
    assert spy.alpha == pytest.approx(0.4)


def test_cash_benchmark_works():
    repo = _repo()
    start = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="Cash benchmark trade.",
        actual_entry=200.0,
        actual_exit=220.0,
        pnl=20.0,
        human_notes=None,
        mistake_tags=[],
        change_reason="Cash benchmark test.",
    )

    result = BenchmarkPerformanceService(repo).compare(
        start=start,
        benchmarks=("CASH",),
    )
    cash = result.comparisons["CASH"]

    assert result.success is True
    assert cash.available is True
    assert cash.missing_data_reason is None
    assert cash.benchmark_return == 0.0
    assert cash.portfolio_return == 0.1
    assert cash.alpha == 0.1


def test_missing_benchmark_data_is_reported():
    repo = _repo()
    start = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="Missing benchmark trade.",
        actual_entry=100.0,
        actual_exit=105.0,
        pnl=5.0,
        human_notes=None,
        mistake_tags=[],
        change_reason="Missing benchmark test.",
    )

    result = BenchmarkPerformanceService(repo).compare(
        start=start,
        benchmarks=("QQQ",),
    )
    qqq = result.comparisons["QQQ"]

    assert qqq.available is False
    assert qqq.missing_data_reason is not None
    assert "QQQ" in qqq.missing_data_reason
    assert qqq.benchmark_return is None
    assert qqq.alpha is None


def test_no_fake_benchmark_data_generated():
    repo = _repo()
    start = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="No fake benchmark trade.",
        actual_entry=100.0,
        actual_exit=101.0,
        pnl=1.0,
        human_notes=None,
        mistake_tags=[],
        change_reason="No fake benchmark test.",
    )

    result = BenchmarkPerformanceService(repo).compare(
        start=start,
        benchmarks=("GLD", "SLV"),
    )

    for symbol in ("GLD", "SLV"):
        comparison = result.comparisons[symbol]
        assert comparison.available is False
        assert comparison.missing_data_reason is not None
        assert comparison.benchmark_return is None
        assert comparison.alpha is None
