from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from trading_system.app.core.enums import EnvironmentMode, OrderStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.learning.recommendations import LearningRecommendationEngine


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _signal(repo: TradingRepository, *, signal_id: str, direction: str = "LONG") -> models.Signal:
    signal = models.Signal(
        id=signal_id,
        idempotency_key=f"{signal_id}-key",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction=direction,
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=98.0,
        target_1=104.0,
        target_2=108.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Breaks stop",
        status="SUBMITTED",
        signal_rule_version="test",
        source_timestamp=datetime(2026, 6, 3, 14, 30, tzinfo=UTC),
    )
    repo.session.add(signal)
    repo.session.commit()
    return signal


def _filled_order(
    repo: TradingRepository,
    *,
    signal: models.Signal,
    side: str,
    quantity: float,
    price: float,
    slippage_bps: float,
    commission: float,
    timestamp: datetime,
    key: str,
) -> None:
    order = repo.store_order(
        PaperOrder(
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            order_type="limit",
            limit_price=price,
            stop_loss=signal.stop_loss,
            idempotency_key=key,
            status=OrderStatus.FILLED,
            reason=f"{side} filled",
            created_at=timestamp,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=timestamp,
    )
    repo.session.add(
        models.Fill(
            order_id=order.id,
            broker_fill_id=f"{key}-fill",
            symbol=signal.symbol,
            quantity=quantity,
            price=price,
            slippage_bps=slippage_bps,
            commission=commission,
            source_timestamp=timestamp,
        )
    )
    repo.session.commit()


def _clean_candle(
    repo: TradingRepository,
    *,
    timestamp: datetime,
    high: float,
    low: float,
    close: float,
) -> None:
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": timestamp,
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": timestamp,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test",
        }
    )


def test_journal_lifecycle_metrics_are_calculated_and_persisted_on_full_exit():
    repo = _repo()
    signal = _signal(repo, signal_id="journal-full-exit")
    entry_at = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    exit_at = entry_at + timedelta(hours=1)

    _filled_order(
        repo,
        signal=signal,
        side="buy",
        quantity=10,
        price=100.0,
        slippage_bps=5.0,
        commission=1.0,
        timestamp=entry_at,
        key="journal-entry",
    )
    _clean_candle(repo, timestamp=entry_at + timedelta(minutes=15), high=106.0, low=98.0, close=105.0)
    _filled_order(
        repo,
        signal=signal,
        side="sell",
        quantity=10,
        price=104.0,
        slippage_bps=7.0,
        commission=2.0,
        timestamp=exit_at,
        key="journal-exit",
    )
    _clean_candle(repo, timestamp=exit_at + timedelta(minutes=5), high=120.0, low=90.0, close=119.0)

    result = repo.persist_journal_lifecycle_for_signal(signal_id=signal.id)
    journal = repo.latest_journal(1)[0]

    assert result["created"] is True
    assert journal["actual_entry"] == 100.0
    assert journal["actual_exit"] == 104.0
    assert journal["pnl"] == 37.0
    assert journal["max_favorable_excursion"] == 60.0
    assert journal["max_adverse_excursion"] == -20.0
    assert journal["slippage_bps"] == 6.0
    assert journal["time_in_trade_seconds"] == 3600.0


def test_learning_worker_has_no_order_intent_surface(monkeypatch):
    repo = _repo()
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="rule violation sample",
        actual_entry=100.0,
        actual_exit=99.0,
        pnl=-10.0,
        human_notes=None,
        mistake_tags=[],
        rule_violations=["STOP_LOSS_BREACHED"],
        slippage_bps=12.0,
        change_reason="test journal with violation",
    )

    def fail_order_intent(*_args, **_kwargs):
        raise AssertionError("learning worker must not emit order intents")

    monkeypatch.setattr(repo, "store_order", fail_order_intent)
    engine = LearningRecommendationEngine(repo)

    assert not hasattr(engine.repository, "store_order")
    assert not hasattr(engine.repository, "mark_order_broker_result")
    assert not hasattr(engine.repository, "update_order_from_broker")

    result = engine.run_weekly_review()
    weekly = repo.latest_weekly_reviews(1)[0]
    recommendations = repo.latest_strategy_recommendations(10)

    assert result.recommendations_created >= 1
    assert repo.counts()["orders"] == 0
    assert "orders" not in weekly["metrics"]
    assert "fills" not in weekly["metrics"]
    assert all("order intent" not in row["recommendation"].lower() for row in recommendations)
