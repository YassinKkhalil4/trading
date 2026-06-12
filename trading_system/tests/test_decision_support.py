from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from trading_system.app.ai.decision_support import (
    DeterministicDecisionSupportProvider,
    build_artifact_payload,
    validate_decision_support_output,
)
from trading_system.app.catalysts.catalyst_engine import classify_news_catalyst_taxonomy
from trading_system.app.core.config import Settings
from trading_system.app.core.enums import MarketRegime, ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.journal.review_engine import TradeReviewEngine
from trading_system.app.learning.recommendations import LearningRecommendationEngine
from trading_system.app.services.ranking.opportunity_ranking import OpportunityRankingService


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        bar_freshness_max_seconds=90,
        provider_health_max_age_seconds=180,
        scheduler_regime_seconds=60,
    )


def _seed_ranking(repo: TradingRepository, now: datetime) -> models.ScannerResult:
    repo.store_provider_health_snapshot(
        provider_name="alpaca_market_data",
        status=ProviderHealthStatus.HEALTHY.value,
        reliability_score=95.0,
        reason="decision-support test provider health",
        source_timestamp=now,
    )
    repo.store_market_regime_snapshot(
        market_regime=MarketRegime.BULL_TREND.value,
        confidence=82.0,
        allowed_bias="LONG_PREFERRED",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="decision-support test regime",
        source_timestamp=now,
    )
    repo.store_intraday_features(
        symbol="AMD",
        source_timestamp=now,
        feature_version="decision-support-test",
        price=100.0,
        vwap=99.0,
        atr=2.0,
        relative_volume=2.0,
        gap_pct=1.0,
        volume_spike_score=80.0,
        liquidity_score=90.0,
        spread_score=95.0,
    )
    return repo.store_generic_scanner_result(
        scanner_name="VWAP_RECLAIM",
        scanner_rule_version="decision_support_test_v1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        accepted=True,
        score=92.0,
        reason="Accepted scanner result for decision-support scorecard test.",
        payload={
            "preflight": {
                "strategy": {
                    "status": StrategyStatus.PAPER_TESTING.value,
                    "allowed_regimes": [MarketRegime.BULL_TREND.value],
                },
                "provider": "alpaca_market_data",
                "provider_health": {
                    "status": ProviderHealthStatus.HEALTHY.value,
                    "reliability_score": 95.0,
                    "source_timestamp": now.isoformat(),
                },
                "latest_data_timestamp": now.isoformat(),
                "timeframe": "1Min",
                "regime": {
                    "market_regime": MarketRegime.BULL_TREND.value,
                    "confidence": 82.0,
                    "source_timestamp": now.isoformat(),
                },
            },
            "relative_strength_20d": 4.0,
        },
        source_timestamp=now,
    )


def test_decision_support_validation_rejects_order_intent_but_allows_disclaimer():
    provider = DeterministicDecisionSupportProvider()
    output = provider.build_trade_thesis(
        {
            "symbol": "AMD",
            "setup_name": "VWAP_RECLAIM",
            "scanner_reason": "VWAP reclaim setup accepted.",
            "market_context": "BULL_TREND",
            "catalyst_summary": None,
        }
    )
    artifact = build_artifact_payload(
        artifact_type="TRADE_THESIS",
        provider=provider,
        prompt_version="test",
        input_payload={"symbol": "AMD", "strategy_id": "VWAP_RECLAIM"},
        output=output,
    )

    assert artifact.validation.accepted is True
    unsafe = validate_decision_support_output({"text": "Submit an order for AMD."})
    assert unsafe.accepted is False


def test_trade_review_persists_decision_support_artifact_without_order_surface(monkeypatch):
    repo = _repo()
    journal = repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="test thesis",
        actual_entry=100.0,
        actual_exit=101.0,
        pnl=10.0,
        human_notes=None,
        mistake_tags=["late_entry"],
        rule_violations=[],
        slippage_bps=4.0,
        change_reason="test journal",
    )

    def fail_order_intent(*_args, **_kwargs):
        raise AssertionError("decision-support review must not emit order intents")

    monkeypatch.setattr(repo, "store_order", fail_order_intent)
    result = TradeReviewEngine(repo).run_once()

    assert result.reviews_created == 1
    review = repo.latest_ai_reviews(1)[0]
    assert review["trade_journal_id"] == journal.id
    assert review["decision_support_artifact_id"]
    assert review["structured_payload"]["disclaimer"].startswith("Decision support only")
    assert repo.counts()["orders"] == 0


def test_learning_recommendations_include_evidence_severity_and_artifact():
    repo = _repo()
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="test thesis",
        actual_entry=100.0,
        actual_exit=99.0,
        pnl=-10.0,
        human_notes=None,
        mistake_tags=[],
        rule_violations=["STOP_LOSS_BREACHED"],
        slippage_bps=12.0,
        change_reason="test journal",
    )

    result = LearningRecommendationEngine(repo).run_weekly_review()
    recommendations = repo.latest_strategy_recommendations(10)

    assert result.recommendations_created >= 1
    assert all(row["decision_support_artifact_id"] for row in recommendations)
    assert any(row["severity"] == "HIGH" for row in recommendations)
    assert all(row["evidence"]["journal_entries"] == 1 for row in recommendations)
    assert repo.counts()["orders"] == 0


def test_opportunity_ranking_persists_scorecard_snapshot():
    repo = _repo()
    now = datetime.now(UTC)
    scanner_result = _seed_ranking(repo, now)

    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)
    snapshots = repo.latest_opportunity_scorecards(10)

    assert ranking.blocked_reason is None
    assert ranking.component_scores["scanner"] == 92.0
    assert snapshots[0]["scanner_result_id"] == scanner_result.id
    assert snapshots[0]["component_scores"]["scanner"] == 92.0
    assert snapshots[0]["grade_rationale"]


def test_catalyst_taxonomy_applies_freshness_decay():
    now = datetime(2026, 6, 12, tzinfo=UTC)
    fresh = classify_news_catalyst_taxonomy(
        "AMD wins major partnership",
        source_timestamp=now,
        now=now,
    )
    stale = classify_news_catalyst_taxonomy(
        "AMD wins major partnership",
        source_timestamp=now - timedelta(days=10),
        now=now,
    )

    assert fresh["catalyst_type"] == "news_momentum"
    assert fresh["materiality_score"] > stale["materiality_score"]
    assert stale["freshness_multiplier"] == 0.25
