from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import desc, select
from sqlalchemy.orm import sessionmaker

from trading_system.app.attribution.performance_attribution import (
    JournalTrade,
    PerformanceAttributionService,
    StrategyMetadata,
)
from trading_system.app.core.config import Settings
from trading_system.app.core.enums import (
    EnvironmentMode,
    MarketRegime,
    OrderStatus,
    ProviderHealthStatus,
    StrategyStatus,
)
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperOrderResult, AlpacaPaperSyncResult
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.execution.fill_reconciliation import FillReconciliationLoop
from trading_system.app.monitoring.trade_monitor_service import TradeMonitorService
from trading_system.app.scanners.production_scanners import ProductionScannerEngine, PRODUCTION_DATA_PROVIDER
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
)
from trading_system.app.services.orchestrators.data_pipeline_orchestrator import DataPipelineOrchestrator
from trading_system.app.services.orchestrators.execution_orchestrator import ExecutionOrchestrator
from trading_system.app.services.orchestrators.research_orchestrator import ResearchOrchestrator
from trading_system.app.services.orchestrators.risk_and_sync_orchestrator import RiskAndSyncOrchestrator


class TradingRuntimeService(
    DataPipelineOrchestrator,
    ExecutionOrchestrator,
    RiskAndSyncOrchestrator,
    ResearchOrchestrator,
):
    """Test-only compatibility facade over physically extracted orchestrators."""
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
        environment_mode=EnvironmentMode.PAPER,
        max_spread_bps=500.0,
        alpaca_paper_api_key="paper-key",
        alpaca_paper_secret_key="paper-secret",
        max_slippage_bps=1_000.0,
    )


def _approve_all_strategies(repo: TradingRepository) -> None:
    rows = repo.session.scalars(select(models.StrategyRegistry)).all()
    for row in rows:
        row.status = StrategyStatus.PAPER_TESTING.value
    repo.session.commit()


def _insert_vwap_reclaim_candles(
    repo: TradingRepository,
    *,
    symbol: str = "AMD",
    latest_at: datetime | None = None,
) -> datetime:
    latest_at = latest_at or datetime.now(UTC)
    start = latest_at - timedelta(minutes=9)
    for idx in range(10):
        ts = start + timedelta(minutes=idx)
        close = 99.0 if idx < 9 else 101.5
        volume = 1_000_000 if idx < 9 else 5_000_000
        raw_id = repo.store_raw_candle(
            {
                "provider": PRODUCTION_DATA_PROVIDER,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx, "shape": "vwap_reclaim"},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": PRODUCTION_DATA_PROVIDER,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": volume,
                "trade_count": None,
                "vwap": 100.0,
                "data_quality_status": "VALID",
                "quality_reason": "integration test candle",
            }
        )
    return latest_at


def _seed_intraday_features(repo: TradingRepository, *, now: datetime) -> None:
    repo.store_intraday_features(
        symbol="AMD",
        source_timestamp=now,
        feature_version="pipeline-integration-test",
        price=101.5,
        vwap=100.0,
        atr=2.5,
        relative_volume=2.2,
        gap_pct=1.5,
        volume_spike_score=80.0,
        liquidity_score=92.0,
        spread_score=88.0,
    )


def _seed_scanner_environment(repo: TradingRepository, *, now: datetime) -> None:
    _approve_all_strategies(repo)
    _insert_vwap_reclaim_candles(repo, latest_at=now)
    repo.store_feature_snapshot(
        symbol="AMD",
        source_timestamp=now,
        feature_version="integration-test",
        snapshot={"relative_strength_20d": 3.5},
    )
    repo.store_provider_health_snapshot(
        provider_name=PRODUCTION_DATA_PROVIDER,
        status=ProviderHealthStatus.HEALTHY.value,
        reason="pipeline integration test provider health",
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
        reason="pipeline integration test regime",
        source_timestamp=now,
    )
    _seed_intraday_features(repo, now=now)


def _latest_scanner_result(repo: TradingRepository, scanner_name: str) -> models.ScannerResult:
    row = repo.session.scalar(
        select(models.ScannerResult)
        .where(models.ScannerResult.scanner_name == scanner_name)
        .order_by(desc(models.ScannerResult.created_at))
        .limit(1)
    )
    assert row is not None
    return row


class FakePaperSubmitAdapter:
    calls: list[dict] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def submit_limit_bracket_order(self, **kwargs) -> AlpacaPaperOrderResult:
        self.__class__.calls.append(kwargs)
        return AlpacaPaperOrderResult(
            configured=True,
            submitted=True,
            reason="fake paper order submitted",
            broker_order_id=f"broker-paper-{len(self.__class__.calls)}",
            payload={"client_order_id": kwargs["client_order_id"]},
        )

    async def sync(self) -> AlpacaPaperSyncResult:
        return AlpacaPaperSyncResult(
            configured=False,
            success=False,
            reason="fake paper adapter sync not used in submit test",
            account=None,
            positions=[],
            orders=[],
        )


class FakeAlpacaPaperSyncAdapter:
    def __init__(self, sync_result: AlpacaPaperSyncResult) -> None:
        self.sync_result = sync_result

    async def sync(self) -> AlpacaPaperSyncResult:
        return self.sync_result


class LiveAdapterMustNotBeCalled:
    def __init__(self, *_args, **_kwargs) -> None:
        raise AssertionError("AlpacaLiveAdapter must not be called in paper pipeline integration test")


def _assert_snapshot_has_reasons(snapshot: models.DecisionLog) -> None:
    payload = snapshot.payload or {}
    reasons = payload.get("reasons") or []
    assert reasons, f"Expected reasons on {payload.get('snapshot_stage')} snapshot"
    assert all(str(reason).strip() for reason in reasons)


@pytest.mark.asyncio
async def test_full_decision_pipeline_vwap_reclaim_with_mocked_alpaca_paper(monkeypatch):
    repo = _repo()
    settings = _settings()
    now = datetime.now(UTC)
    _seed_scanner_environment(repo, now=now)

    scanner_run = ProductionScannerEngine(repo, settings).run_once(["AMD"])
    scanner_result = _latest_scanner_result(repo, "VWAP_RECLAIM")
    assert scanner_run.scanners_run >= 1
    assert scanner_result.accepted is True
    assert scanner_result.strategy_id == "VWAP_RECLAIM"
    assert repo.active_strategy_cooldown(symbol="AMD", strategy_id="VWAP_RECLAIM", now=now) is None

    ranking_service = OpportunityRankingService(repo, settings)
    ranking = ranking_service.rank_scanner_result(scanner_result, now)
    assert ranking.blocked_reason is None
    assert ranking.grade in {OpportunityGrade.A, OpportunityGrade.A_PLUS}
    assert ranking.reasons

    bridge = ScannerSignalBridgeService(repo, settings, ranking_service=ranking_service)
    low_grade_preflight = build_preflight_payload(
        repo,
        symbol="AMD",
        strategy_id="RELATIVE_STRENGTH",
        timeframe="1Min",
        latest_data_timestamp=now,
    )
    low_grade_result = repo.store_generic_scanner_result(
        scanner_name="RELATIVE_STRENGTH",
        scanner_rule_version="pipeline_integration_low_grade_v1",
        symbol="AMD",
        strategy_id="RELATIVE_STRENGTH",
        accepted=True,
        score=55.0,
        reason="Low-grade scanner result for bridge gate validation.",
        payload={
            "preflight": low_grade_preflight,
            "relative_strength_20d": 1.0,
            "latest_close": 101.5,
            "latest_vwap": 100.0,
        },
        source_timestamp=now,
    )
    low_ranking = ranking_service.rank_scanner_result(low_grade_result, now)
    assert low_ranking.grade not in {OpportunityGrade.A, OpportunityGrade.A_PLUS}
    low_bridge = bridge.try_create_signal(low_grade_result.id, ranking=low_ranking, now=now)
    assert low_bridge.created is False
    assert low_bridge.blocked_reason is not None
    assert "below bridge threshold" in low_bridge.blocked_reason

    bridge_result = bridge.try_create_signal(scanner_result.id, ranking=ranking, now=now)
    assert bridge_result.created is True
    assert bridge_result.signal_id is not None
    assert bridge_result.signal is not None

    signal_row = repo.signal_by_id(bridge_result.signal_id)
    assert signal_row is not None
    trade_signal = bridge_result.signal.to_trade_signal()

    portfolio_service = PortfolioDecisionService(settings=settings, repository=repo)
    portfolio_decision = portfolio_service.evaluate(
        signal_id=bridge_result.signal_id,
        signal=trade_signal,
        context=PortfolioEvaluationContext(
            account_equity=100_000,
            account_cash=100_000,
        ),
        persist=True,
    )
    assert portfolio_decision.reasons
    assert portfolio_decision.approved is True

    snapshot_service = DecisionSnapshotService(repo)
    ranking_snapshots = snapshot_service.list_snapshots(
        stage=DecisionSnapshotStage.OPPORTUNITY_RANKING,
        entity_id=scanner_result.id,
    )
    signal_snapshots = snapshot_service.list_snapshots(
        stage=DecisionSnapshotStage.SIGNAL_CREATION,
        entity_id=bridge_result.signal_id,
    )
    portfolio_snapshots = snapshot_service.list_snapshots(
        stage=DecisionSnapshotStage.PORTFOLIO_DECISION,
        entity_id=bridge_result.signal_id,
    )
    assert len(ranking_snapshots) == 1
    assert len(signal_snapshots) == 1
    assert len(portfolio_snapshots) == 1
    for snapshot in ranking_snapshots + signal_snapshots + portfolio_snapshots:
        _assert_snapshot_has_reasons(snapshot)

    assert repo.counts()["orders"] == 0
    FakePaperSubmitAdapter.calls = []
    monkeypatch.setattr(
        "trading_system.app.services.orchestrators.execution_orchestrator.AlpacaPaperAdapter",
        FakePaperSubmitAdapter,
    )
    monkeypatch.setattr(
        "trading_system.app.services.orchestrators.execution_orchestrator.AlpacaLiveAdapter",
        LiveAdapterMustNotBeCalled,
    )
    monkeypatch.setattr(
        "trading_system.app.execution.fill_reconciliation.AlpacaLiveAdapter",
        LiveAdapterMustNotBeCalled,
    )

    runtime = TradingRuntimeService(repo, settings=settings)
    submit_result = await runtime.submit_signal_to_paper(
        signal_id=bridge_result.signal_id,
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
        trades_today=0,
        strategy_trades_today=0,
        internal_quantity=0,
        broker_quantity=0,
    )

    risk_snapshots = snapshot_service.list_snapshots(
        stage=DecisionSnapshotStage.RISK_DECISION,
        entity_id=bridge_result.signal_id,
    )
    assert len(risk_snapshots) == 1
    _assert_snapshot_has_reasons(risk_snapshots[0])
    assert portfolio_snapshots[0].created_at <= risk_snapshots[0].created_at

    assert submit_result["risk_check"]["approved"] is True
    assert submit_result["risk_check"]["reason"]
    assert submit_result["order"]["status"] == "SUBMITTED"
    assert submit_result["broker_submit"]["submitted"] is True
    assert len(FakePaperSubmitAdapter.calls) == 1
    assert repo.counts()["orders"] == 1

    order = repo.latest_orders(1)[0]
    broker_order = {
        "id": submit_result["broker_submit"]["broker_order_id"],
        "client_order_id": order["idempotency_key"],
        "symbol": "AMD",
        "side": "buy",
        "qty": str(order["quantity"]),
        "type": "limit",
        "limit_price": f"{order['limit_price']:.2f}",
        "status": "filled",
        "filled_qty": str(order["quantity"]),
        "filled_avg_price": f"{order['limit_price']:.2f}",
        "filled_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    fill_sync = AlpacaPaperSyncResult(
        configured=True,
        success=True,
        reason="integration test fill sync",
        account={"equity": "100000"},
        positions=[],
        orders=[broker_order],
    )
    fill_result = await FillReconciliationLoop(
        repo,
        settings,
        adapter=FakeAlpacaPaperSyncAdapter(fill_sync),
    ).run_once()
    assert fill_result.fills_recorded == 1
    assert repo.counts()["fills"] == 1

    persisted_order = repo.session.scalar(
        select(models.Order).where(models.Order.idempotency_key == order["idempotency_key"])
    )
    assert persisted_order is not None
    assert persisted_order.side == "buy"

    journal = repo.latest_journal(1)[0]
    assert journal["signal_id"] == bridge_result.signal_id
    assert journal["actual_entry"] == pytest.approx(float(order["limit_price"]))
    assert "entry fill reconciliation" in journal["change_reason"].lower()

    monitor_result = TradeMonitorService(repo).run_once()
    assert monitor_result.journal_entries_updated >= 0

    refreshed_journal = repo.latest_journal(1)[0]
    assert refreshed_journal["signal_id"] == journal["signal_id"]

    attribution = PerformanceAttributionService().attribute(
        journal_entries=[
            JournalTrade(
                symbol=journal["symbol"],
                strategy_id=journal["strategy_id"],
                pnl=journal.get("pnl"),
                max_adverse_excursion=journal.get("max_adverse_excursion"),
                time_in_trade_seconds=journal.get("time_in_trade_seconds"),
            )
        ],
        strategy_metadata={
            "VWAP_RECLAIM": StrategyMetadata("VWAP_RECLAIM", "VWAP Reclaim"),
        },
    )
    assert attribution.by_strategy["VWAP_RECLAIM"].trade_count == 1


def test_duplicate_scanner_emission_blocked_after_acceptance():
    repo = _repo()
    settings = _settings()
    now = datetime.now(UTC)
    _seed_scanner_environment(repo, now=now)

    ProductionScannerEngine(repo, settings).run_once(["AMD"])
    first = _latest_scanner_result(repo, "VWAP_RECLAIM")
    ProductionScannerEngine(repo, settings).run_once(["AMD"])
    second = _latest_scanner_result(repo, "VWAP_RECLAIM")

    assert first.accepted is True
    assert second.accepted is False
    assert "duplicate scanner emission" in second.reason.lower()


@pytest.mark.asyncio
async def test_journal_lifecycle_records_buy_sell_for_long_entry_exit(monkeypatch):
    repo = _repo()
    settings = _settings()
    now = datetime.now(UTC)
    _seed_scanner_environment(repo, now=now)

    ProductionScannerEngine(repo, settings).run_once(["AMD"])
    scanner_result = _latest_scanner_result(repo, "VWAP_RECLAIM")
    ranking = OpportunityRankingService(repo, settings).rank_scanner_result(scanner_result, now)
    bridge_result = ScannerSignalBridgeService(repo, settings).try_create_signal(
        scanner_result.id,
        ranking=ranking,
        now=now,
    )
    assert bridge_result.signal_id is not None

    monkeypatch.setattr(
        "trading_system.app.services.orchestrators.execution_orchestrator.AlpacaPaperAdapter",
        FakePaperSubmitAdapter,
    )
    runtime = TradingRuntimeService(repo, settings=settings)
    await runtime.submit_signal_to_paper(
        signal_id=bridge_result.signal_id,
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
        trades_today=0,
        strategy_trades_today=0,
    )
    entry_order = repo.latest_orders(1)[0]
    assert entry_order["side"] == "buy"

    entry_at = now
    exit_at = now + timedelta(hours=1)
    repo.session.add(
        models.Fill(
            order_id=entry_order["id"],
            broker_fill_id="journal-entry-buy",
            symbol="AMD",
            quantity=entry_order["quantity"],
            price=entry_order["limit_price"],
            slippage_bps=5.0,
            commission=0.0,
            source_timestamp=entry_at,
        )
    )
    exit_order = repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="sell",
            quantity=entry_order["quantity"],
            order_type="limit",
            limit_price=105.0,
            stop_loss=98.0,
            idempotency_key="journal-exit-sell",
            status=OrderStatus.FILLED,
            reason="long exit sell fill",
            created_at=exit_at,
        ),
        signal_id=bridge_result.signal_id,
        strategy_id="VWAP_RECLAIM",
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=exit_at,
    )
    repo.session.add(
        models.Fill(
            order_id=exit_order.id,
            broker_fill_id="journal-exit-sell",
            symbol="AMD",
            quantity=entry_order["quantity"],
            price=105.0,
            slippage_bps=4.0,
            commission=0.0,
            source_timestamp=exit_at,
        )
    )
    repo.session.commit()

    lifecycle = repo.persist_journal_lifecycle_for_signal(signal_id=bridge_result.signal_id)
    journal = repo.latest_journal(1)[0]

    assert lifecycle["created"] is True
    assert journal["actual_entry"] == entry_order["limit_price"]
    assert journal["actual_exit"] == 105.0
    assert journal["pnl"] == (105.0 - entry_order["limit_price"]) * entry_order["quantity"]
