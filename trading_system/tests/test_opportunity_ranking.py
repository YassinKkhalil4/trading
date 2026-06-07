from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import CatalystDirection, MarketRegime, ProviderHealthStatus, StrategyStatus
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
        reason="opportunity ranking test provider health",
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
        reason="opportunity ranking test regime",
        source_timestamp=now,
    )
    return now


def _store_intraday_features(
    repo: TradingRepository,
    *,
    now: datetime,
    liquidity_score: float = 92.0,
    spread_score: float = 88.0,
) -> None:
    repo.store_intraday_features(
        symbol="AMD",
        source_timestamp=now,
        feature_version="opportunity-ranking-test",
        price=110.0,
        vwap=105.0,
        atr=2.5,
        relative_volume=2.2,
        gap_pct=1.5,
        volume_spike_score=80.0,
        liquidity_score=liquidity_score,
        spread_score=spread_score,
    )


def _store_accepted_scanner_result(
    repo: TradingRepository,
    *,
    strategy_id: str,
    now: datetime,
    latest_data_timestamp: datetime | None = None,
    scanner_score: float = 92.0,
    catalyst_id: str | None = None,
    relative_strength_20d: float = 4.0,
    provider_status: str = ProviderHealthStatus.HEALTHY.value,
    provider_reliability: float = 100.0,
) -> models.ScannerResult:
    latest = latest_data_timestamp or now
    if provider_status != ProviderHealthStatus.HEALTHY.value or provider_reliability != 100.0:
        repo.store_provider_health_snapshot(
            provider_name=PRODUCTION_DATA_PROVIDER,
            status=provider_status,
            reason="opportunity ranking test provider override",
            reliability_score=provider_reliability,
            source_timestamp=now,
        )
    preflight = build_preflight_payload(
        repo,
        symbol="AMD",
        strategy_id=strategy_id,
        timeframe="1Min",
        latest_data_timestamp=latest,
    )
    payload = {
        "preflight": preflight,
        "relative_strength_20d": relative_strength_20d,
        "catalyst_id": catalyst_id,
    }
    return repo.store_generic_scanner_result(
        scanner_name=strategy_id,
        scanner_rule_version="opportunity_ranking_test_v1",
        symbol="AMD",
        strategy_id=strategy_id,
        accepted=True,
        score=scanner_score,
        reason="Accepted scanner result for ranking test.",
        payload=payload,
        source_timestamp=now,
    )


def test_stale_data_lowers_or_rejects_ranking():
    repo = _repo()
    now = _seed_common(repo)
    service = OpportunityRankingService(repo, _settings())

    fresh_result = _store_accepted_scanner_result(repo, strategy_id="VWAP_RECLAIM", now=now)
    fresh_ranking = service.rank_scanner_result(fresh_result, now)

    stale_result = _store_accepted_scanner_result(
        repo,
        strategy_id="OPENING_RANGE_BREAKOUT",
        now=now,
        latest_data_timestamp=now - timedelta(days=3),
    )
    stale_ranking = service.rank_scanner_result(stale_result, now)

    aged_result = _store_accepted_scanner_result(
        repo,
        strategy_id="RELATIVE_STRENGTH",
        now=now,
        latest_data_timestamp=now - timedelta(seconds=300),
    )
    aged_ranking = service.rank_scanner_result(aged_result, now)

    assert stale_ranking.grade == OpportunityGrade.REJECT
    assert stale_ranking.blocked_reason == "Market data is stale for scanner timeframe."
    assert stale_ranking.opportunity_score == 0.0
    assert fresh_ranking.opportunity_score > aged_ranking.opportunity_score


def test_unhealthy_provider_rejects_ranking():
    repo = _repo()
    now = _seed_common(repo)
    service = OpportunityRankingService(repo, _settings())

    result = _store_accepted_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        provider_status=ProviderHealthStatus.DOWN.value,
        provider_reliability=0.0,
    )
    ranking = service.rank_scanner_result(result, now)

    assert ranking.grade == OpportunityGrade.REJECT
    assert ranking.blocked_reason == "Provider health is DOWN."
    assert ranking.opportunity_score == 0.0


def test_strong_candidate_ranks_a_or_a_plus():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    service = OpportunityRankingService(repo, _settings())

    result = _store_accepted_scanner_result(
        repo,
        strategy_id="OPENING_RANGE_BREAKOUT",
        now=now,
        scanner_score=95.0,
        relative_strength_20d=4.5,
    )
    ranking = service.rank_scanner_result(result, now)

    assert ranking.blocked_reason is None
    assert ranking.opportunity_score >= 78.0
    assert ranking.grade in {OpportunityGrade.A, OpportunityGrade.A_PLUS}


def test_missing_optional_catalyst_does_not_reject_non_catalyst_strategy():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    service = OpportunityRankingService(repo, _settings())

    result = _store_accepted_scanner_result(
        repo,
        strategy_id="VWAP_RECLAIM",
        now=now,
        catalyst_id=None,
    )
    ranking = service.rank_scanner_result(result, now)

    assert ranking.blocked_reason is None
    assert ranking.grade != OpportunityGrade.REJECT
    assert ranking.opportunity_score > 0.0


def test_catalyst_required_strategy_rejected_without_catalyst():
    repo = _repo()
    now = _seed_common(repo)
    service = OpportunityRankingService(repo, _settings())

    result = _store_accepted_scanner_result(
        repo,
        strategy_id="NEWS_MOMENTUM",
        now=now,
        catalyst_id=None,
    )
    ranking = service.rank_scanner_result(result, now)

    assert ranking.grade == OpportunityGrade.REJECT
    assert ranking.blocked_reason == "Required catalyst is missing for catalyst-dependent strategy."
    assert ranking.opportunity_score == 0.0
