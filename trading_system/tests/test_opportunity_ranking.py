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
    RankingInputs,
    build_preflight_payload,
    compute_opportunity_ranking,
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


def _settings_with(**overrides) -> Settings:
    return Settings(
        bar_freshness_max_seconds=600,
        provider_health_max_age_seconds=600,
        scheduler_regime_seconds=60,
        **overrides,
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


def _full_inputs(now: datetime, **overrides) -> RankingInputs:
    """A candidate that clears every hard block, with all components maxed.

    Override individual fields/components to exercise specific scoring rules.
    """
    base = dict(
        scanner_result_id="rank-1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        scanner_name="VWAP_RECLAIM",
        scanner_score=100.0,
        strategy_status=StrategyStatus.PAPER_TESTING.value,
        allowed_regimes=frozenset(),
        cooldown_active=False,
        cooldown_until=None,
        cooldown_reason=None,
        provider=PRODUCTION_DATA_PROVIDER,
        provider_health_status=ProviderHealthStatus.HEALTHY.value,
        provider_health_reliability=100.0,
        provider_health_timestamp=now,
        latest_data_timestamp=now,
        timeframe="1Min",
        market_regime=MarketRegime.BULL_TREND.value,
        regime_confidence=100.0,
        regime_timestamp=now,
        catalyst_id="cat-1",
        catalyst_materiality_score=100.0,
        relative_strength_20d=5.0,
        liquidity_score=100.0,
        spread_score=100.0,
        now=now,
    )
    base.update(overrides)
    return RankingInputs(**base)


def test_perfect_candidate_scores_exactly_100():
    now = datetime.now(UTC)
    ranking = compute_opportunity_ranking(_full_inputs(now), _settings())

    assert ranking.blocked_reason is None
    assert ranking.opportunity_score == 100.0
    assert ranking.grade == OpportunityGrade.A_PLUS


def test_score_is_normalized_to_0_100_regardless_of_weight_sum():
    now = datetime.now(UTC)
    # Weights that do not sum to 100 must still yield a 0-100 score.
    settings = _settings_with(
        ranking_weight_scanner=1.0,
        ranking_weight_freshness=1.0,
        ranking_weight_provider=1.0,
        ranking_weight_regime=1.0,
        ranking_weight_catalyst=1.0,
        ranking_weight_relative_strength=1.0,
        ranking_weight_liquidity=1.0,
        ranking_weight_spread=1.0,
    )
    ranking = compute_opportunity_ranking(_full_inputs(now), settings)
    assert ranking.opportunity_score == 100.0


def test_configurable_weights_drive_the_score():
    now = datetime.now(UTC)
    # Put all weight on the scanner component: the score must equal it.
    settings = _settings_with(
        ranking_weight_scanner=100.0,
        ranking_weight_freshness=0.0,
        ranking_weight_provider=0.0,
        ranking_weight_regime=0.0,
        ranking_weight_catalyst=0.0,
        ranking_weight_relative_strength=0.0,
        ranking_weight_liquidity=0.0,
        ranking_weight_spread=0.0,
    )
    ranking = compute_opportunity_ranking(_full_inputs(now, scanner_score=73.0), settings)
    assert ranking.opportunity_score == 73.0


def test_configurable_grade_thresholds():
    now = datetime.now(UTC)
    weight_on_scanner = dict(
        ranking_weight_scanner=100.0,
        ranking_weight_freshness=0.0,
        ranking_weight_provider=0.0,
        ranking_weight_regime=0.0,
        ranking_weight_catalyst=0.0,
        ranking_weight_relative_strength=0.0,
        ranking_weight_liquidity=0.0,
        ranking_weight_spread=0.0,
    )
    inputs = _full_inputs(now, scanner_score=73.0)

    default_like = compute_opportunity_ranking(inputs, _settings_with(**weight_on_scanner))
    assert default_like.grade == OpportunityGrade.B

    stricter = compute_opportunity_ranking(
        inputs,
        _settings_with(ranking_grade_b_min=80.0, ranking_grade_watch_min=50.0, **weight_on_scanner),
    )
    assert stricter.grade == OpportunityGrade.WATCH


def test_relative_strength_multiplier_is_configurable():
    now = datetime.now(UTC)
    weight_on_rs = dict(
        ranking_weight_scanner=0.0,
        ranking_weight_freshness=0.0,
        ranking_weight_provider=0.0,
        ranking_weight_regime=0.0,
        ranking_weight_catalyst=0.0,
        ranking_weight_relative_strength=100.0,
        ranking_weight_liquidity=0.0,
        ranking_weight_spread=0.0,
    )
    inputs = _full_inputs(now, relative_strength_20d=2.0)

    low = compute_opportunity_ranking(
        inputs, _settings_with(ranking_relative_strength_multiplier=20.0, **weight_on_rs)
    )
    high = compute_opportunity_ranking(
        inputs, _settings_with(ranking_relative_strength_multiplier=50.0, **weight_on_rs)
    )
    assert low.opportunity_score == 40.0
    assert high.opportunity_score == 100.0


def test_missing_relative_strength_is_neutral_not_zero():
    now = datetime.now(UTC)
    weight_on_rs = dict(
        ranking_weight_scanner=0.0,
        ranking_weight_freshness=0.0,
        ranking_weight_provider=0.0,
        ranking_weight_regime=0.0,
        ranking_weight_catalyst=0.0,
        ranking_weight_relative_strength=100.0,
        ranking_weight_liquidity=0.0,
        ranking_weight_spread=0.0,
    )
    settings = _settings_with(ranking_neutral_component_score=50.0, **weight_on_rs)

    missing = compute_opportunity_ranking(_full_inputs(now, relative_strength_20d=None), settings)
    genuine_zero = compute_opportunity_ranking(_full_inputs(now, relative_strength_20d=0.0), settings)

    assert missing.opportunity_score == 50.0
    assert genuine_zero.opportunity_score == 0.0


def test_missing_provider_reliability_uses_unknown_default_but_zero_is_preserved():
    now = datetime.now(UTC)
    weight_on_provider = dict(
        ranking_weight_scanner=0.0,
        ranking_weight_freshness=0.0,
        ranking_weight_provider=100.0,
        ranking_weight_regime=0.0,
        ranking_weight_catalyst=0.0,
        ranking_weight_relative_strength=0.0,
        ranking_weight_liquidity=0.0,
        ranking_weight_spread=0.0,
    )
    settings = _settings_with(ranking_unknown_provider_reliability=100.0, **weight_on_provider)

    missing = compute_opportunity_ranking(_full_inputs(now, provider_health_reliability=None), settings)
    genuine_zero = compute_opportunity_ranking(_full_inputs(now, provider_health_reliability=0.0), settings)

    assert missing.opportunity_score == 100.0
    assert genuine_zero.opportunity_score == 0.0
