from __future__ import annotations

import pytest

import json
import sys
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

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
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperOrderResult
from trading_system.app.features.production_features import ProductionFeatureEngine
from trading_system.app.scanners.production_scanners import PRODUCTION_DATA_PROVIDER
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
from trading_system.app.strategies.registry import StrategyRegistryService


def _repo():
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    return TradingRepository(session)


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

    async def submit_market_order(self, **kwargs) -> AlpacaPaperOrderResult:
        self.__class__.calls.append(kwargs)
        return AlpacaPaperOrderResult(
            configured=True,
            submitted=True,
            reason="fake paper market order submitted",
            broker_order_id=f"broker-paper-market-{len(self.__class__.calls)}",
            payload={"client_order_id": kwargs["client_order_id"], "type": "market"},
        )


def test_bootstrap_seeds_real_runtime_tables():
    repo = _repo()
    service = TradingRuntimeService(repo)
    counts = service.bootstrap()
    assert counts["symbols"] == 5
    providers = repo.list_rows(__import__("trading_system.app.db.models", fromlist=["ProviderCapability"]).ProviderCapability)
    assert any(row["provider_name"] == "yahoo_chart" for row in providers)


def test_bootstrap_does_not_seed_strategy_into_paper_eligibility():
    repo = _repo()
    TradingRuntimeService(repo).bootstrap()

    strategy = repo.session.query(models.StrategyRegistry).filter_by(strategy_id="VWAP_RECLAIM").one()
    paper_allowed, reason = StrategyRegistryService().can_paper_trade("VWAP_RECLAIM")

    assert strategy.status == StrategyStatus.RESEARCH.value
    assert paper_allowed is False
    assert "RESEARCH" in reason


def test_bootstrap_does_not_auto_create_schema_outside_local_sqlite(monkeypatch):
    repo = _repo()
    calls = {"create_schema": 0}

    def fake_create_schema() -> None:
        calls["create_schema"] += 1

    monkeypatch.setattr(repo, "create_schema", fake_create_schema)
    service = TradingRuntimeService(
        repo,
        settings=Settings(
            deployment_target="production",
            database_url="postgresql+psycopg://example",
        ),
    )

    service.bootstrap()

    assert calls["create_schema"] == 0


def test_runtime_scan_persists_signal_from_clean_candles():
    repo = _repo()
    service = TradingRuntimeService(repo, settings=Settings(max_spread_bps=500.0))
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
    assert result.scanner_result_id is not None
    assert result.signal_id is not None
    snapshot = service.system_snapshot()
    assert snapshot["counts"]["signals"] == 1
    assert snapshot["signals"][0]["id"] == result.signal_id


def _store_scannable_candles(repo: TradingRepository) -> list[datetime]:
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
    return timestamps


def _approve_all_strategies(repo: TradingRepository) -> None:
    for row in repo.session.scalars(select(models.StrategyRegistry)).all():
        row.status = StrategyStatus.PAPER_TESTING.value
    repo.session.commit()


def test_runtime_scan_routes_through_ranking_bridge_when_flag_enabled():
    repo = _repo()
    service = TradingRuntimeService(
        repo,
        settings=Settings(
            max_spread_bps=500.0,
            enable_ranking_signal_path=True,
            # Make any non-blocked candidate grade A_PLUS so the wiring (not the
            # exact score) is what this test exercises; scoring math is covered
            # in test_opportunity_ranking.py.
            ranking_grade_a_plus_min=1.0,
            bar_freshness_max_seconds=86_400,
            provider_health_max_age_seconds=86_400,
            scheduler_regime_seconds=3_600,
        ),
    )
    service.bootstrap()
    timestamps = _store_scannable_candles(repo)
    _approve_all_strategies(repo)
    repo.store_provider_health_snapshot(
        provider_name=PRODUCTION_DATA_PROVIDER,
        status=ProviderHealthStatus.HEALTHY.value,
        reason="runtime ranking wiring test provider health",
        reliability_score=100.0,
        source_timestamp=timestamps[-1],
    )
    repo.store_market_regime_snapshot(
        market_regime=MarketRegime.BULL_TREND.value,
        confidence=95.0,
        allowed_bias="long",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="runtime ranking wiring test regime",
        source_timestamp=timestamps[-1],
    )

    result = service.run_vwap_scan("AMD")

    assert result.scanner_result_id is not None
    assert result.signal_id is not None
    assert result.reason == "Ranked signal generated; deprecated trade thesis persistence has been removed."
    # The bridge path (and only the bridge path) records a strategy cooldown when
    # it creates a signal, so its presence proves we routed through the bridge.
    cooldowns = repo.session.scalars(select(models.StrategyCooldown)).all()
    assert any(
        "Signal created from ranked scanner opportunity" in (row.reason or "")
        for row in cooldowns
    )


def test_runtime_scan_ranking_path_blocks_without_provider_health():
    repo = _repo()
    service = TradingRuntimeService(
        repo,
        settings=Settings(
            max_spread_bps=500.0,
            enable_ranking_signal_path=True,
            ranking_grade_a_plus_min=1.0,
        ),
    )
    service.bootstrap()
    _store_scannable_candles(repo)
    _approve_all_strategies(repo)
    # Deliberately omit provider-health and regime snapshots: the ranking gate must
    # hard-block, so the scan accepts the scanner result but creates no signal.

    result = service.run_vwap_scan("AMD")

    assert result.scanner_result_id is not None
    assert result.signal_id is None
    assert result.thesis_id is None
    assert result.reason is not None
    assert "Provider health" in result.reason
    assert repo.session.scalars(select(models.Signal)).all() == []
    assert repo.session.scalars(select(models.StrategyCooldown)).all() == []


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


@pytest.mark.asyncio
async def test_submit_signal_to_paper_records_exposure_and_daily_loss_kill_switch():
    repo = _repo()
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        max_spread_bps=500.0,
        max_daily_loss_pct=1.0,
    )
    signal_id = _store_runtime_signal(repo, settings)
    service = TradingRuntimeService(repo, settings=settings)

    result = await service.submit_signal_to_paper(
        signal_id=signal_id,
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=1.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
        symbol_exposure_pct=12.0,
        strategy_exposure_pct=18.0,
        correlated_exposure_pct=22.0,
        overnight_exposure_pct=5.0,
        trades_today=0,
        strategy_trades_today=0,
        internal_quantity=0,
        broker_quantity=0,
    )

    exposure = repo.latest_exposure_snapshots(1)[0]
    kill_switch = repo.latest_kill_switches(1)[0]

    assert result["risk_check"]["approved"] is False
    assert exposure["symbol_exposure"]["AMD"] == 12.0
    assert exposure["strategy_exposure"]["VWAP_RECLAIM"] == 18.0
    assert exposure["symbol_exposure"]["correlated"] == 22.0
    assert exposure["symbol_exposure"]["overnight"] == 5.0
    assert kill_switch["event_type"] == "DAILY_LOSS_LIMIT"
    assert repo.active_kill_switch_count() == 1


def test_broker_equity_loss_pct_uses_snapshots_inside_lookback():
    repo = _repo()
    now = datetime.now(tz=ZoneInfo("America/New_York"))
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": "100000", "buying_power": "200000"},
        reason="baseline",
        source_timestamp=now - timedelta(hours=3),
    )
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": "97000", "buying_power": "180000"},
        reason="latest",
        source_timestamp=now,
    )

    loss_pct = repo.broker_equity_loss_pct(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        lookback=timedelta(days=1),
    )

    assert loss_pct == 3.0


def test_broker_payloads_are_archived_when_raw_archive_bucket_is_configured(monkeypatch):
    repo = _repo()
    archived_objects: list[dict] = []

    class FakeS3Client:
        def put_object(self, **kwargs):
            archived_objects.append(kwargs)

    def fake_client(service_name: str, **_kwargs):
        assert service_name == "s3"
        return FakeS3Client()

    monkeypatch.setenv("RAW_ARCHIVE_BUCKET", "trading-raw-archive")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))

    repo.store_broker_sync(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        success=False,
        mismatch_detected=True,
        reason="position mismatch",
        payload={"broker_positions": [{"symbol": "AMD", "qty": "10"}]},
    )
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": "100000", "buying_power": "200000"},
        reason="account sync",
    )

    assert len(archived_objects) == 2
    archive_payloads = [
        json.loads(archive["Body"].decode("utf-8"))
        for archive in archived_objects
    ]
    assert {payload["category"] for payload in archive_payloads} == {"broker_sync", "broker_account"}
    assert all(archive["Bucket"] == "trading-raw-archive" for archive in archived_objects)
    assert all(archive["ServerSideEncryption"] == "AES256" for archive in archived_objects)
    assert archive_payloads[0]["payload"]["payload"]["broker_positions"][0]["symbol"] == "AMD"
    assert archive_payloads[1]["payload"]["account"]["id"] == "paper-account"
    audit_events = {row["event_type"] for row in repo.latest_audit_logs(5)}
    assert "RAW_PAYLOAD_ARCHIVED" in audit_events


def test_broker_order_updates_are_archived_when_raw_archive_bucket_is_configured(monkeypatch):
    repo = _repo()
    archived_objects: list[dict] = []

    class FakeS3Client:
        def put_object(self, **kwargs):
            archived_objects.append(kwargs)

    def fake_client(service_name: str, **_kwargs):
        assert service_name == "s3"
        return FakeS3Client()

    monkeypatch.setenv("RAW_ARCHIVE_BUCKET", "trading-raw-archive")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))

    order = repo.update_order_from_broker(
        broker_order={
            "id": "live-order-123",
            "client_order_id": "live-client-123",
            "symbol": "AMD",
            "side": "buy",
            "qty": "5",
            "type": "limit",
            "limit_price": "101.25",
            "status": "accepted",
            "updated_at": "2026-06-06T14:30:00Z",
        },
        environment_mode=EnvironmentMode.LIVE.value,
    )

    assert order is not None
    assert len(archived_objects) == 1
    archive_payload = json.loads(archived_objects[0]["Body"].decode("utf-8"))
    assert archive_payload["category"] == "broker_order"
    assert archive_payload["provider"] == "alpaca_live"
    assert archive_payload["symbol"] == "AMD"
    assert archive_payload["payload"]["environment_mode"] == "live"
    assert archive_payload["payload"]["broker_order"]["id"] == "live-order-123"
    assert archived_objects[0]["Key"].startswith(
        "raw/broker_order/provider=alpaca_live/symbol=AMD/date=2026/06/06/"
    )


def test_raw_ingestion_payloads_are_archived_with_distinct_utc_timestamps(monkeypatch):
    repo = _repo()
    archived_objects: list[dict] = []

    class FakeS3Client:
        def put_object(self, **kwargs):
            archived_objects.append(kwargs)

    def fake_client(service_name: str, **_kwargs):
        assert service_name == "s3"
        return FakeS3Client()

    monkeypatch.setenv("RAW_ARCHIVE_BUCKET", "trading-raw-archive")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))

    source_timestamp = datetime(2026, 6, 3, 9, 31, tzinfo=ZoneInfo("America/New_York"))
    received_at = datetime(2026, 6, 3, 13, 31, 2, tzinfo=UTC)
    repo.enqueue_raw_market_bar(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": source_timestamp,
            "received_at": received_at,
            "raw_payload": {"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000},
        }
    )
    repo.enqueue_raw_trade_tick(
        provider="alpaca_market_data",
        symbol="AMD",
        trade_id="trade-1",
        source_timestamp=source_timestamp,
        received_at=received_at,
        price=100.25,
        size=100,
        raw_payload={"i": "trade-1", "p": 100.25, "s": 100},
    )
    repo.enqueue_raw_news(
        provider="unit_news",
        symbol="AMD",
        headline="AMD unit test headline",
        url="https://example.test/news",
        source_timestamp=source_timestamp,
        raw_payload={"headline": "AMD unit test headline"},
    )

    archive_payloads = [json.loads(item["Body"].decode("utf-8")) for item in archived_objects]
    assert {payload["category"] for payload in archive_payloads} == {
        "market_data",
        "trade_ticks",
        "news",
    }
    assert all(payload["source_timestamp"].endswith("+00:00") for payload in archive_payloads)
    assert all(payload["received_at"].endswith("+00:00") for payload in archive_payloads)
    assert all(payload["processed_at"].endswith("+00:00") for payload in archive_payloads)
    assert repo.counts()["raw_ingestion_events"] == 3
    assert repo.counts()["raw_trade_ticks"] == 1


def test_feature_store_blocks_when_clean_candle_status_is_invalid():
    repo = _repo()
    timestamps = [
        datetime(2026, 6, 3, 13, 30, tzinfo=UTC),
        datetime(2026, 6, 3, 13, 31, tzinfo=UTC),
        datetime(2026, 6, 3, 13, 32, tzinfo=UTC),
    ]
    candles = [
        {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.0, "volume": 1000.0},
        {"open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0, "volume": 1100.0},
        {"open": 140.0, "high": 142.0, "low": 139.0, "close": 141.0, "volume": 1200.0},
    ]
    for timestamp, candle in zip(timestamps, candles, strict=True):
        raw_id = repo.store_raw_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": timestamp,
                "raw_payload": candle,
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": timestamp,
                "data_quality_status": "VALID",
                "quality_reason": "test",
                **candle,
            }
        )

    result = ProductionFeatureEngine(repo).run_once(["AMD"])

    assert any(row["data_quality_status"] == "SUSPICIOUS_PRICE" for row in repo.latest_clean_candles(3))
    assert result.intraday_snapshots == 0
    assert repo.latest_features(1) == []


@pytest.mark.asyncio
async def test_submit_signal_to_paper_uses_broker_equity_loss_for_kill_switch():
    repo = _repo()
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        max_spread_bps=500.0,
        max_daily_loss_pct=1.0,
    )
    signal_id = _store_runtime_signal(repo, settings)
    now = datetime.now(tz=ZoneInfo("America/New_York"))
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": "100000", "buying_power": "200000"},
        reason="baseline",
        source_timestamp=now - timedelta(hours=2),
    )
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.PAPER.value,
        broker="alpaca_paper",
        account={"id": "paper-account", "equity": "98500", "buying_power": "190000"},
        reason="latest",
        source_timestamp=now,
    )
    service = TradingRuntimeService(repo, settings=settings)

    result = await service.submit_signal_to_paper(
        signal_id=signal_id,
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

    kill_switch = repo.latest_kill_switches(1)[0]

    assert result["risk_check"]["approved"] is False
    assert result["risk_check"]["reason"] == "Max daily loss reached."
    assert result["risk_check"]["payload"]["broker_daily_loss_pct"] == 1.5
    assert result["order"]["status"] == "REJECTED"
    assert kill_switch["event_type"] == "DAILY_LOSS_LIMIT"
    assert kill_switch["payload"]["symbol"] == "AMD"


@pytest.mark.asyncio
async def test_submit_signal_to_paper_triggers_reconciliation_kill_switch():
    repo = _repo()
    settings = Settings(environment_mode=EnvironmentMode.PAPER, max_spread_bps=500.0)
    signal_id = _store_runtime_signal(repo, settings)
    service = TradingRuntimeService(repo, settings=settings)

    result = await service.submit_signal_to_paper(
        signal_id=signal_id,
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
        trades_today=0,
        strategy_trades_today=0,
        internal_quantity=0,
        broker_quantity=5,
    )

    kill_switch = repo.latest_kill_switches(1)[0]

    assert result["reconciliation"]["ok"] is False
    assert result["risk_check"]["approved"] is False
    assert kill_switch["event_type"] == "FAILED_RECONCILIATION"


@pytest.mark.asyncio
async def test_submit_signal_to_paper_triggers_volatility_kill_switch():
    repo = _repo()
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        max_spread_bps=500.0,
        max_volatility_score=50.0,
    )
    signal_id = _store_runtime_signal(repo, settings)
    service = TradingRuntimeService(repo, settings=settings)
    repo.store_daily_features(
        symbol="AMD",
        source_timestamp=datetime.now(tz=ZoneInfo("America/New_York")),
        feature_version="test",
        atr=12.0,
        atr_pct=12.0,
        gap_pct=0.0,
        trend_score=50.0,
        volatility_score=80.0,
        liquidity_score=100.0,
    )

    result = await service.submit_signal_to_paper(
        signal_id=signal_id,
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

    kill_switch = repo.latest_kill_switches(1)[0]
    audit = next(
        row for row in repo.latest_audit_logs(10)
        if row["event_type"] == "KILL_SWITCH_ACTIVATED"
        and row["entity_id"] == kill_switch["id"]
    )

    assert result["risk_check"]["approved"] is False
    assert result["risk_check"]["reason"] == "Volatility score exceeds configured limit."
    assert result["order"]["status"] == "REJECTED"
    assert result["broker_submit"] is None
    assert kill_switch["event_type"] == "VOLATILITY_BREACH"
    assert audit["payload"]["volatility_score"] == 80.0
    assert repo.active_kill_switch_count() == 1


@pytest.mark.asyncio
async def test_duplicate_paper_submit_is_rejected_before_second_broker_call(monkeypatch):
    repo = _repo()
    settings = Settings(environment_mode=EnvironmentMode.PAPER, max_spread_bps=500.0)
    signal_id = _store_runtime_signal(repo, settings)
    service = TradingRuntimeService(repo, settings=settings)
    FakePaperSubmitAdapter.calls = []
    monkeypatch.setattr(
        "trading_system.app.services.orchestrators.execution_orchestrator.AlpacaPaperAdapter",
        FakePaperSubmitAdapter,
    )

    first = await service.submit_signal_to_paper(
        signal_id=signal_id,
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
    second = await service.submit_signal_to_paper(
        signal_id=signal_id,
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

    error = repo.latest_execution_errors(1)[0]

    assert first["broker_submit"]["submitted"] is True
    assert second["broker_submit"]["submitted"] is False
    assert len(FakePaperSubmitAdapter.calls) == 1
    assert repo.counts()["orders"] == 1
    assert error["order_id"] == first["order"]["id"]
    assert error["error_type"] == "DUPLICATE_PAPER_ORDER"


@pytest.mark.asyncio
async def test_internal_paper_market_order_submits_to_broker_with_idempotency(monkeypatch):
    repo = _repo()
    settings = Settings(environment_mode=EnvironmentMode.PAPER)
    order = models.Order(
        signal_id=None,
        idempotency_key="protective-exit-paper-test",
        environment_mode=EnvironmentMode.PAPER.value,
        execution_environment="PAPER",
        broker="alpaca_paper",
        broker_order_id=None,
        symbol="AMD",
        side="sell",
        quantity=10,
        order_type="market",
        limit_price=None,
        stop_loss=None,
        status=OrderStatus.SUBMITTED.value,
        expected_price=97.5,
        source_timestamp=datetime.now(ZoneInfo("UTC")),
    )
    repo.session.add(order)
    repo.session.commit()
    FakePaperSubmitAdapter.calls = []
    monkeypatch.setattr(
        "trading_system.app.services.orchestrators.execution_orchestrator.AlpacaPaperAdapter",
        FakePaperSubmitAdapter,
    )

    result = await TradingRuntimeService(repo, settings=settings).submit_internal_order_to_broker(
        order_id=order.id,
        actor="unit-test",
        reason="submit protective exit",
    )

    updated = repo.latest_orders(1)[0]

    assert result["accepted"] is True
    assert result["broker_submit"]["submitted"] is True
    assert FakePaperSubmitAdapter.calls == [
        {
            "symbol": "AMD",
            "side": "sell",
            "quantity": 10,
            "client_order_id": "protective-exit-paper-test",
        }
    ]
    assert updated["broker_order_id"] == "broker-paper-market-1"
    assert repo.latest_broker_sync_logs(1)[0]["success"] is True
