from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import MarketRegime, ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.scanners.production_scanners import PRODUCTION_DATA_PROVIDER
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityGrade,
    OpportunityRankingService,
    build_preflight_payload,
)
from trading_system.app.services.signals.scanner_signal_bridge import ScannerSignalBridgeService


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _settings() -> Settings:
    return Settings(
        bar_freshness_max_seconds=600,
        provider_health_max_age_seconds=600,
        scheduler_regime_seconds=60,
    )


def _approve_all(repo: TradingRepository) -> None:
    rows = repo.session.scalars(select(models.StrategyRegistry)).all()
    for row in rows:
        row.status = StrategyStatus.PAPER_TESTING.value
    repo.session.commit()


def _seed_common(repo: TradingRepository, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    _approve_all(repo)
    repo.store_provider_health_snapshot(
        provider_name=PRODUCTION_DATA_PROVIDER,
        status=ProviderHealthStatus.HEALTHY.value,
        reason="scanner signal bridge test provider health",
        reliability_score=100.0,
        source_timestamp=now,
    )
    repo.store_market_regime_snapshot(
        market_regime=MarketRegime.BULL_TREND.value,
        confidence=95.0,
        allowed_bias="long",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="scanner signal bridge test regime",
        source_timestamp=now,
    )
    return now


def _store_intraday_features(repo: TradingRepository, *, now: datetime) -> None:
    repo.store_intraday_features(
        symbol="AMD",
        source_timestamp=now,
        feature_version="scanner-signal-bridge-test",
        price=110.0,
        vwap=105.0,
        atr=2.5,
        relative_volume=2.2,
        gap_pct=1.5,
        volume_spike_score=80.0,
        liquidity_score=92.0,
        spread_score=88.0,
    )


def _store_scanner_result(
    repo: TradingRepository,
    *,
    strategy_id: str,
    now: datetime,
    accepted: bool = True,
    latest_data_timestamp: datetime | None = None,
    scanner_score: float = 92.0,
    relative_strength_20d: float = 4.5,
    latest_close: float = 110.0,
    latest_vwap: float = 105.0,
    cooldown: models.StrategyCooldown | None = None,
    provider_status: str = ProviderHealthStatus.HEALTHY.value,
    provider_reliability: float = 100.0,
) -> models.ScannerResult:
    latest = latest_data_timestamp or now
    if provider_status != ProviderHealthStatus.HEALTHY.value or provider_reliability != 100.0:
        repo.store_provider_health_snapshot(
            provider_name=PRODUCTION_DATA_PROVIDER,
            status=provider_status,
            reason="scanner signal bridge provider override",
            reliability_score=provider_reliability,
            source_timestamp=now,
        )
    preflight = build_preflight_payload(
        repo,
        symbol="AMD",
        strategy_id=strategy_id,
        timeframe="1Min",
        latest_data_timestamp=latest,
        cooldown=cooldown,
    )
    payload = {
        "preflight": preflight,
        "relative_strength_20d": relative_strength_20d,
        "latest_close": latest_close,
        "latest_vwap": latest_vwap,
    }
    return repo.store_generic_scanner_result(
        scanner_name=strategy_id,
        scanner_rule_version="scanner_signal_bridge_test_v1",
        symbol="AMD",
        strategy_id=strategy_id,
        accepted=accepted,
        score=scanner_score,
        reason="Scanner result for bridge test.",
        payload=payload,
        source_timestamp=now,
    )


def _bridge(repo: TradingRepository) -> ScannerSignalBridgeService:
    settings = _settings()
    return ScannerSignalBridgeService(
        repo,
        settings,
        ranking_service=OpportunityRankingService(repo, settings),
    )


def test_rejected_scanner_result_cannot_create_signal():
    repo = _repo()
    now = _seed_common(repo)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        accepted=False,
    )

    result = _bridge(repo).try_create_signal(scanner_result.id, now=now)

    assert result.created is False
    assert result.signal is None
    assert result.blocked_reason is not None
    assert "Scanner result rejected" in result.blocked_reason


def test_b_or_watch_grade_cannot_create_signal():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="RELATIVE_STRENGTH",
        now=now,
        scanner_score=55.0,
        relative_strength_20d=1.0,
    )
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)
    assert ranking.grade in {OpportunityGrade.B, OpportunityGrade.WATCH, OpportunityGrade.REJECT}

    result = _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)

    assert result.created is False
    assert result.signal is None
    assert result.blocked_reason is not None
    assert "below bridge threshold" in result.blocked_reason


def test_signal_creation_stores_strategy_cooldown():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        scanner_score=95.0,
        relative_strength_20d=4.5,
    )
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)

    result = _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)

    assert result.created is True
    cooldown = repo.active_strategy_cooldown(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        now=now,
    )
    assert cooldown is not None
    assert "Signal created from ranked scanner opportunity" in cooldown.reason


def test_a_or_a_plus_grade_can_create_signal():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="OPENING_RANGE_BREAKOUT",
        now=now,
        scanner_score=95.0,
        relative_strength_20d=4.5,
    )
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)
    assert ranking.grade in {OpportunityGrade.A, OpportunityGrade.A_PLUS}

    result = _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)

    assert result.created is True
    assert result.signal is not None
    assert result.signal_id is not None
    assert result.signal.scanner_result_id == scanner_result.id
    assert result.signal.symbol == "AMD"
    assert result.signal.strategy_id == "OPENING_RANGE_BREAKOUT"
    assert result.signal.confidence_score == ranking.opportunity_score
    assert result.signal.regime_reference is not None

    signal_row = repo.signal_by_id(result.signal_id)
    assert signal_row is not None
    assert signal_row.idempotency_key == result.signal.idempotency_key


def test_duplicate_idempotency_is_blocked():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        scanner_score=95.0,
        relative_strength_20d=4.5,
    )

    bridge = _bridge(repo)
    first = bridge.try_create_signal(scanner_result.id, now=now)
    second = bridge.try_create_signal(scanner_result.id, now=now)

    assert first.created is True
    assert second.created is False
    assert second.blocked_reason is not None
    assert "Duplicate idempotency key rejected" in second.blocked_reason


def test_cooldown_blocks_signal_creation():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    cooldown = repo.store_strategy_cooldown(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        cooldown_until=now + timedelta(minutes=30),
        reason="Recent stop-out cooldown.",
        source_timestamp=now,
    )
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        scanner_score=95.0,
        relative_strength_20d=4.5,
        cooldown=cooldown,
    )

    result = _bridge(repo).try_create_signal(scanner_result.id, now=now)

    assert result.created is False
    assert result.blocked_reason is not None
    assert "cooldown active" in result.blocked_reason.lower()


def test_stale_data_blocks_signal_creation():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        latest_data_timestamp=now - timedelta(days=3),
        scanner_score=95.0,
        relative_strength_20d=4.5,
    )

    result = _bridge(repo).try_create_signal(scanner_result.id, now=now)

    assert result.created is False
    assert result.blocked_reason is not None
    assert "stale" in result.blocked_reason.lower()
