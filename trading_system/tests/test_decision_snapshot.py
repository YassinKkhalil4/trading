from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import (
    EnvironmentMode,
    MarketRegime,
    ProviderHealthStatus,
    StrategyStatus,
)
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.scanners.production_scanners import PRODUCTION_DATA_PROVIDER
from trading_system.app.services.portfolio.portfolio_engine import (
    PortfolioDecisionService,
    PortfolioEvaluationContext,
)
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityGrade,
    OpportunityRankingService,
    build_preflight_payload,
)
from trading_system.app.services.replay.decision_snapshot_service import (
    DecisionSnapshotService,
    DecisionSnapshotStage,
    FORBIDDEN_SNAPSHOT_KEYS,
)
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.services.signals.scanner_signal_bridge import ScannerSignalBridgeService
from trading_system.app.signals.signal_engine import TradeSignal
from trading_system.app.core.enums import Direction, SignalStatus, TradeType


def _seed_authoritative_paper_state(repo: TradingRepository, *, equity: float = 100_000.0) -> None:
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": str(equity), "cash": str(equity), "buying_power": str(equity * 4)},
        reason="Seed authoritative paper account state for execution tests.",
        source_timestamp=datetime.now(UTC),
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
        environment_mode=EnvironmentMode.PAPER,
        max_spread_bps=500.0,
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
        reason="decision snapshot test provider health",
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
        reason="decision snapshot test regime",
        source_timestamp=now,
    )
    return now


def _store_scanner_result(
    repo: TradingRepository,
    *,
    strategy_id: str,
    now: datetime,
) -> models.ScannerResult:
    preflight = build_preflight_payload(
        repo,
        symbol="AMD",
        strategy_id=strategy_id,
        timeframe="1Min",
        latest_data_timestamp=now,
    )
    payload = {
        "preflight": preflight,
        "relative_strength_20d": 4.5,
        "latest_close": 110.0,
        "latest_vwap": 105.0,
    }
    return repo.store_generic_scanner_result(
        scanner_name=strategy_id,
        scanner_rule_version="decision_snapshot_test_v1",
        symbol="AMD",
        strategy_id=strategy_id,
        accepted=True,
        score=95.0,
        reason="Scanner result for decision snapshot test.",
        payload=payload,
        source_timestamp=now,
    )


def _store_intraday_features(repo: TradingRepository, *, now: datetime) -> None:
    repo.store_intraday_features(
        symbol="AMD",
        source_timestamp=now,
        feature_version="decision-snapshot-test",
        price=110.0,
        vwap=105.0,
        atr=2.5,
        relative_volume=2.2,
        gap_pct=1.5,
        volume_spike_score=80.0,
        liquidity_score=92.0,
        spread_score=88.0,
    )


def _bridge(repo: TradingRepository) -> ScannerSignalBridgeService:
    settings = _settings()
    return ScannerSignalBridgeService(
        repo,
        settings,
        ranking_service=OpportunityRankingService(repo, settings),
    )


def _signal(symbol: str = "AMD") -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(110.0, 110.5),
        stop_loss=105.0,
        target_1=120.0,
        target_2=125.0,
        risk_reward=2.0,
        confidence_score=90.0,
        time_horizon="intraday",
        invalidation="test invalidation",
        source_timestamp=datetime.now(UTC),
        idempotency_key="decision-snapshot-signal-key",
        status=SignalStatus.CANDIDATE,
        rule_version="signal_rules_v1",
    )


def _store_runtime_signal(repo: TradingRepository, settings: Settings) -> str:
    service = TradingRuntimeService(repo, settings=settings)
    service.bootstrap()
    timestamps = [
        datetime(2026, 6, 3, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        datetime(2026, 6, 3, 10, 1, tzinfo=ZoneInfo("America/New_York")),
    ]
    candles = [
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 99.0, "volume": 100_000.0},
        {"open": 99.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 4_000_000.0},
    ]
    for ts, candle in zip(timestamps, candles, strict=True):
        raw_id = repo.store_raw_candle(
            {
                "provider": "yahoo_chart",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": candle,
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "yahoo_chart",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": "test candle",
                **candle,
            }
        )
    result = service.run_vwap_scan("AMD")
    assert result.signal_id is not None
    return result.signal_id


def test_signal_snapshot_created_on_bridge_success():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(repo, strategy_id="OPENING_RANGE_BREAKOUT", now=now)
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)
    assert ranking.grade in {OpportunityGrade.A, OpportunityGrade.A_PLUS}

    result = _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)
    assert result.created is True
    assert result.signal_id is not None

    snapshots = DecisionSnapshotService(repo).list_snapshots(
        stage=DecisionSnapshotStage.SIGNAL_CREATION,
        entity_id=result.signal_id,
    )
    assert len(snapshots) == 1
    payload = snapshots[0].payload
    assert payload["snapshot_stage"] == DecisionSnapshotStage.SIGNAL_CREATION.value
    assert payload["decision_result"]["created"] is True
    assert payload["entity_refs"]["signal_id"] == result.signal_id


def test_risk_snapshot_created_on_paper_submit():
    repo = _repo()
    settings = _settings()
    signal_id = _store_runtime_signal(repo, settings)
    _seed_authoritative_paper_state(repo)
    service = TradingRuntimeService(repo, settings=settings)

    service.submit_signal_to_paper(
        signal_id=signal_id,
    )

    snapshots = DecisionSnapshotService(repo).list_snapshots(
        stage=DecisionSnapshotStage.RISK_DECISION,
        entity_id=signal_id,
    )
    assert len(snapshots) == 1
    payload = snapshots[0].payload
    assert payload["snapshot_stage"] == DecisionSnapshotStage.RISK_DECISION.value
    assert "approved" in payload["decision_result"]
    assert "portfolio_state" in payload["decision_result"]


def test_snapshot_includes_feature_regime_and_provider_state():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(repo, strategy_id="VWAP_RECLAIM", now=now)
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)

    _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)

    ranking_snapshots = DecisionSnapshotService(repo).list_snapshots(
        stage=DecisionSnapshotStage.OPPORTUNITY_RANKING,
        entity_id=scanner_result.id,
    )
    assert len(ranking_snapshots) == 1
    payload = ranking_snapshots[0].payload

    assert payload["feature_values"]["latest_close"] == 110.0
    assert payload["feature_values"]["latest_vwap"] == 105.0
    assert payload["feature_values"]["relative_strength_20d"] == 4.5
    assert payload["regime_state"]["market_regime"] == MarketRegime.BULL_TREND.value
    assert payload["provider_health"]["status"] == ProviderHealthStatus.HEALTHY.value
    assert payload["data_freshness"]["latest_data_timestamp"] is not None
    assert payload["reasons"]


def test_snapshot_does_not_include_later_trade_outcome():
    repo = _repo()
    now = _seed_common(repo)
    _store_intraday_features(repo, now=now)
    scanner_result = _store_scanner_result(repo, strategy_id="VWAP_RECLAIM", now=now)
    ranking = OpportunityRankingService(repo, _settings()).rank_scanner_result(scanner_result, now)
    bridge_result = _bridge(repo).try_create_signal(scanner_result.id, ranking=ranking, now=now)
    assert bridge_result.signal_id is not None

    snapshot_service = DecisionSnapshotService(repo)
    signal_snapshot = snapshot_service.list_snapshots(
        stage=DecisionSnapshotStage.SIGNAL_CREATION,
        entity_id=bridge_result.signal_id,
    )[0]
    captured_at = signal_snapshot.created_at

    repo.store_journal_entry(
        signal_id=bridge_result.signal_id,
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="Later trade outcome that must not leak into snapshot.",
        actual_entry=110.0,
        actual_exit=115.0,
        pnl=500.0,
        human_notes="trade_outcome=WIN",
        mistake_tags=[],
        change_reason="Closed trade after snapshot capture.",
    )

    stored_payload = signal_snapshot.payload
    for forbidden_key in FORBIDDEN_SNAPSHOT_KEYS:
        assert forbidden_key not in stored_payload
        assert forbidden_key not in str(stored_payload)

    refreshed = repo.session.get(models.DecisionLog, signal_snapshot.id)
    assert refreshed is not None
    assert refreshed.created_at == captured_at
    assert "trade_outcome" not in (refreshed.payload or {})
    assert "pnl" not in (refreshed.payload or {})


def test_portfolio_decision_snapshot_created_when_persisted():
    repo = _repo()
    service = PortfolioDecisionService(settings=_settings(), repository=repo)
    decision = service.evaluate(
        signal_id="sig-snapshot-portfolio",
        signal=_signal(),
        context=PortfolioEvaluationContext(
            account_equity=100_000,
            account_cash=100_000,
        ),
        persist=True,
    )

    snapshots = DecisionSnapshotService(repo).list_snapshots(
        stage=DecisionSnapshotStage.PORTFOLIO_DECISION,
        entity_id="sig-snapshot-portfolio",
    )
    assert len(snapshots) == 1
    payload = snapshots[0].payload
    assert payload["decision_result"]["outcome"] == decision.outcome.value
    assert payload["decision_result"]["recommended_size_multiplier"] == decision.recommended_size_multiplier
