from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from trading_system.app.core.enums import Direction
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.services.ranking.expectancy import (
    EXPECTANCY_RULE_VERSION,
    ExpectancyService,
    ExpectancyView,
    OutcomeRecord,
    _r_multiple,
    compute_stats,
    empty_stats,
)


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return TradingRepository(session)


def _record(
    *,
    symbol: str = "AAPL",
    strategy_id: str | None = "VWAP_RECLAIM",
    sector: str | None = "Technology",
    regime: str | None = "TRENDING",
    direction: str = Direction.LONG.value,
    pnl: float,
    r_multiple: float | None,
    exit_hour_utc: int,
    time_in_trade_seconds: float = 3600.0,
) -> OutcomeRecord:
    exit_at = datetime(2026, 6, 1, exit_hour_utc, 0, tzinfo=UTC)
    entry_at = datetime(2026, 6, 1, max(0, exit_hour_utc - 1), 0, tzinfo=UTC)
    return OutcomeRecord(
        signal_id=f"sig-{symbol}-{exit_hour_utc}",
        symbol=symbol,
        strategy_id=strategy_id,
        sector=sector,
        regime=regime,
        direction=direction,
        actual_entry=100.0,
        actual_exit=100.0 + pnl,
        entry_at=entry_at,
        exit_at=exit_at,
        pnl=pnl,
        r_multiple=r_multiple,
        time_in_trade_seconds=time_in_trade_seconds,
    )


def test_empty_stats_reports_no_fabricated_numbers():
    stats = compute_stats([], matched_on="overall")
    assert stats.sample_size == 0
    assert stats.r_sample_size == 0
    assert stats.win_rate is None
    assert stats.avg_r is None
    assert stats.median_r is None
    assert stats.max_drawdown is None
    assert stats.drawdown_basis is None
    assert stats.avg_time_to_target_seconds is None
    assert stats.failure_rate_before_1030 is None
    assert stats.expectancy is None
    assert stats.matched_on == "overall"
    assert stats.version == EXPECTANCY_RULE_VERSION


def test_r_multiple_long_short_and_guards():
    # Long: (exit - entry) / (entry - stop)
    assert _r_multiple(
        direction=Direction.LONG.value, actual_entry=100.0, actual_exit=110.0, stop_loss=95.0
    ) == 2.0
    # Short: (entry - exit) / (stop - entry)
    assert _r_multiple(
        direction=Direction.SHORT.value, actual_entry=100.0, actual_exit=90.0, stop_loss=105.0
    ) == 2.0
    # Missing stop -> None
    assert _r_multiple(
        direction=Direction.LONG.value, actual_entry=100.0, actual_exit=110.0, stop_loss=None
    ) is None
    # Risk denominator <= 0 (stop on wrong side) -> None
    assert _r_multiple(
        direction=Direction.LONG.value, actual_entry=100.0, actual_exit=110.0, stop_loss=105.0
    ) is None


def test_compute_stats_aggregates_real_outcomes():
    records = [
        _record(pnl=100.0, r_multiple=2.0, exit_hour_utc=15, time_in_trade_seconds=3600.0),
        _record(pnl=-50.0, r_multiple=-1.0, exit_hour_utc=14, time_in_trade_seconds=1800.0),
        _record(pnl=50.0, r_multiple=0.5, exit_hour_utc=16, time_in_trade_seconds=7200.0),
    ]
    stats = compute_stats(records)

    assert stats.sample_size == 3
    assert stats.r_sample_size == 3
    assert stats.win_rate == round(2 / 3, 4)
    assert stats.avg_r == round((2.0 - 1.0 + 0.5) / 3, 4)
    assert stats.median_r == 0.5
    assert stats.expectancy == round((100.0 - 50.0 + 50.0) / 3, 4)
    # Only winners contribute to time-to-target: (3600 + 7200) / 2
    assert stats.avg_time_to_target_seconds == 5400.0


def test_failure_rate_before_1030_uses_eastern_time():
    # 14:00 UTC == 10:00 ET (EDT) -> before 10:30; 15:00 UTC == 11:00 ET -> after.
    early_loser = _record(pnl=-30.0, r_multiple=-1.0, exit_hour_utc=14)
    late_loser = _record(pnl=-20.0, r_multiple=-0.5, exit_hour_utc=15)
    winner = _record(pnl=80.0, r_multiple=1.5, exit_hour_utc=14)
    stats = compute_stats([early_loser, late_loser, winner])
    # Only the early loser counts; denominator is the full sample of 3.
    assert stats.failure_rate_before_1030 == round(1 / 3, 4)


def test_max_drawdown_prefers_r_curve():
    records = [
        _record(pnl=-50.0, r_multiple=-1.0, exit_hour_utc=14),
        _record(pnl=100.0, r_multiple=2.0, exit_hour_utc=15),
        _record(pnl=25.0, r_multiple=0.5, exit_hour_utc=16),
    ]
    stats = compute_stats(records)
    # Ordered by exit: -1.0, +2.0, +0.5 -> cumulative -1.0, 1.0, 1.5 -> worst drop is -1.0 R.
    assert stats.drawdown_basis == "R"
    assert stats.max_drawdown == -1.0


def test_max_drawdown_falls_back_to_pnl_when_no_r():
    records = [
        _record(pnl=100.0, r_multiple=None, exit_hour_utc=14),
        _record(pnl=-40.0, r_multiple=None, exit_hour_utc=15),
    ]
    stats = compute_stats(records)
    assert stats.r_sample_size == 0
    assert stats.drawdown_basis == "pnl"
    assert stats.max_drawdown == -40.0


def test_match_widens_until_records_found():
    records = [
        _record(symbol="AAPL", strategy_id="VWAP_RECLAIM", regime="TRENDING", pnl=10.0, r_multiple=1.0, exit_hour_utc=15),
        _record(symbol="MSFT", strategy_id="VWAP_RECLAIM", regime="CHOPPY", pnl=-10.0, r_multiple=-1.0, exit_hour_utc=15),
    ]
    view = ExpectancyView(records)

    exact = view.match(strategy_id="VWAP_RECLAIM", symbol="AAPL", regime="TRENDING")
    assert exact.matched_on == "strategy+symbol+regime"
    assert exact.sample_size == 1

    # Symbol present but regime has no exact cohort -> widen to strategy+symbol.
    widened = view.match(strategy_id="VWAP_RECLAIM", symbol="AAPL", regime="VOLATILE")
    assert widened.matched_on == "strategy+symbol"
    assert widened.sample_size == 1

    # Unknown strategy -> overall cohort.
    overall = view.match(strategy_id="UNKNOWN", symbol="TSLA", regime="TRENDING")
    assert overall.matched_on == "overall"
    assert overall.sample_size == 2

    # No records at all -> explicit empty.
    assert ExpectancyView([]).match(strategy_id="X", symbol="Y").matched_on == "none"


def test_empty_stats_helper_matches_compute_on_empty():
    assert empty_stats(matched_on="x") == compute_stats([], matched_on="x")


def _add_signal(repo: TradingRepository, *, signal_id: str, symbol: str, stop_loss: float) -> None:
    repo.session.add(
        models.Signal(
            id=signal_id,
            idempotency_key=f"idem-{signal_id}",
            symbol=symbol,
            strategy_id="VWAP_RECLAIM",
            strategy_version="v1",
            trade_type="INTRADAY",
            direction=Direction.LONG.value,
            entry_zone={"low": 99.0, "high": 101.0},
            stop_loss=stop_loss,
            invalidation="thesis broken",
        )
    )


def _add_order_with_fill(
    repo: TradingRepository,
    *,
    order_id: str,
    signal_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    at: datetime,
) -> None:
    repo.session.add(
        models.Order(
            id=order_id,
            signal_id=signal_id,
            idempotency_key=f"idem-{order_id}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            source_timestamp=at,
        )
    )
    repo.session.add(
        models.Fill(
            order_id=order_id,
            symbol=symbol,
            quantity=quantity,
            price=price,
            source_timestamp=at,
        )
    )


def test_service_excludes_partial_exits_and_computes_r_from_fills():
    repo = _repo()

    # Fully closed long winner: entry 100 -> exit 112, stop 96 -> R = 12 / 4 = 3.0
    _add_signal(repo, signal_id="sig-closed", symbol="AAPL", stop_loss=96.0)
    _add_order_with_fill(
        repo,
        order_id="ord-closed-entry",
        signal_id="sig-closed",
        symbol="AAPL",
        side="buy",
        quantity=100.0,
        price=100.0,
        at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )
    _add_order_with_fill(
        repo,
        order_id="ord-closed-exit",
        signal_id="sig-closed",
        symbol="AAPL",
        side="sell",
        quantity=100.0,
        price=112.0,
        at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC),
    )

    # Partially exited (40 of 100) -> must be excluded entirely.
    _add_signal(repo, signal_id="sig-partial", symbol="MSFT", stop_loss=95.0)
    _add_order_with_fill(
        repo,
        order_id="ord-partial-entry",
        signal_id="sig-partial",
        symbol="MSFT",
        side="buy",
        quantity=100.0,
        price=100.0,
        at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )
    _add_order_with_fill(
        repo,
        order_id="ord-partial-exit",
        signal_id="sig-partial",
        symbol="MSFT",
        side="sell",
        quantity=40.0,
        price=108.0,
        at=datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
    )
    repo.session.commit()

    view = ExpectancyService(repo).load()
    assert [record.symbol for record in view.records] == ["AAPL"]

    record = view.records[0]
    assert record.actual_entry == 100.0
    assert record.actual_exit == 112.0
    assert record.r_multiple == 3.0
    assert record.pnl == (112.0 - 100.0) * 100.0
    assert record.time_in_trade_seconds == 5400.0

    summary = view.summary()
    assert summary["overall"].sample_size == 1
    assert "AAPL" in summary["by_symbol"]


def test_service_returns_empty_when_no_trades():
    repo = _repo()
    view = ExpectancyService(repo).load()
    assert view.records == []
    assert view.summary()["overall"].sample_size == 0
