from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.api.main import app
from trading_system.app.api.routers import admin as admin_router
from trading_system.app.core.config import Settings
from trading_system.app.core.enums import EnvironmentMode, ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.risk.kill_switch import KillSwitchService
from trading_system.app.risk.live_readiness import LiveReadinessService
from trading_system.app.security.auth import AdminPrincipal, require_principal


client = TestClient(app)

PASS_CHECKED_AT = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)
HOLIDAY_CHECKED_AT = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)

GATE_NAMES = [
    "environment_mode",
    "live_config_present",
    "live_confirmation_phrase",
    "live_keys_present",
    "admin_session_secret_not_default",
    "provider_health_fresh",
    "alpaca_market_data_fresh",
    "broker_reconciliation_clean",
    "no_active_kill_switch",
    "approved_strategy",
    "data_quality_valid",
    "market_session_valid",
]


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _live_settings(**overrides) -> Settings:
    defaults = dict(
        environment_mode=EnvironmentMode.LIVE,
        allow_live_trading=True,
        confirm_live_trading="I_UNDERSTAND_RISK",
        enable_live_order_path=True,
        alpaca_live_api_key="live-key",
        alpaca_live_secret_key="live-secret",
        admin_session_secret="unit-test-live-session-secret",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_live_ready_at(repo: TradingRepository, settings: Settings, checked_at: datetime) -> None:
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )
    strategy.status = StrategyStatus.APPROVED_SMALL_SIZE.value
    repo.session.commit()
    for provider in ["alpaca_market_data", "alpaca_live"]:
        repo.store_provider_health_snapshot(
            provider_name=provider,
            status=ProviderHealthStatus.HEALTHY.value,
            reason="test healthy",
            reliability_score=100.0,
            source_timestamp=checked_at,
        )
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": checked_at,
            "raw_payload": {"test": "fresh live-readiness candle"},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": checked_at,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 2_000_000,
            "trade_count": None,
            "vwap": 100.3,
            "data_quality_status": "VALID",
            "quality_reason": "fresh Alpaca candle for live readiness",
        }
    )
    repo.store_broker_sync(
        environment_mode=EnvironmentMode.LIVE.value,
        broker="alpaca_live",
        success=True,
        mismatch_detected=False,
        reason="test reconciliation clean",
        payload={},
    )


def _gate_map(result):
    return {gate.gate_name: gate for gate in result.gates}


def test_all_detail_gates_pass_when_live_ready():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready_at(repo, settings, PASS_CHECKED_AT)

    result = LiveReadinessService(repo, settings).get_detail_report(checked_at=PASS_CHECKED_AT)

    assert result.overall_status == "PASSED"
    assert result.checked_at == PASS_CHECKED_AT
    assert len(result.gates) == len(GATE_NAMES)
    assert all(gate.passed for gate in result.gates)
    assert [gate.gate_name for gate in result.gates] == GATE_NAMES


@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_detail_gate_failure_blocks_overall_status(gate_name: str):
    repo = _repo()
    settings = _live_settings()
    checked_at = HOLIDAY_CHECKED_AT if gate_name == "market_session_valid" else PASS_CHECKED_AT
    _make_live_ready_at(repo, settings, checked_at)

    if gate_name == "environment_mode":
        settings = _live_settings(environment_mode=EnvironmentMode.PAPER)
    elif gate_name == "live_config_present":
        settings = _live_settings(allow_live_trading=False)
    elif gate_name == "live_confirmation_phrase":
        settings = _live_settings(confirm_live_trading="NOT_CONFIRMED")
    elif gate_name == "live_keys_present":
        settings = _live_settings(alpaca_live_api_key="")
    elif gate_name == "admin_session_secret_not_default":
        settings = _live_settings(admin_session_secret="change-me")
    elif gate_name == "provider_health_fresh":
        repo.store_provider_health_snapshot(
            provider_name="alpaca_live",
            status=ProviderHealthStatus.DOWN.value,
            reason="unit test broker down",
            reliability_score=0.0,
            source_timestamp=checked_at,
        )
    elif gate_name == "alpaca_market_data_fresh":
        repo.session.query(models.CleanMarketData).delete()
        repo.session.query(models.MarketDataStreamEvent).delete()
        repo.session.commit()
    elif gate_name == "broker_reconciliation_clean":
        repo.store_broker_sync(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
            success=False,
            mismatch_detected=True,
            reason="unit test mismatch",
            payload={},
        )
    elif gate_name == "no_active_kill_switch":
        KillSwitchService(repo).activate(event_type="TEST", reason="unit test", payload={})
    elif gate_name == "approved_strategy":
        strategy = repo.session.scalar(
            select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
        )
        strategy.status = StrategyStatus.PAUSED.value
        repo.session.commit()
    elif gate_name == "data_quality_valid":
        candle = repo.session.scalar(
            select(models.CleanMarketData).where(models.CleanMarketData.provider == "alpaca_market_data")
        )
        candle.data_quality_status = "SUSPICIOUS_PRICE"
        candle.quality_reason = "bad price for detail gate test"
        repo.session.commit()
    elif gate_name == "market_session_valid":
        pass

    result = LiveReadinessService(repo, settings).get_detail_report(checked_at=checked_at)
    gates = _gate_map(result)

    assert result.overall_status == "BLOCKED"
    assert len(result.gates) == len(GATE_NAMES)
    assert gates[gate_name].passed is False
    assert gates[gate_name].blocking_reason


def test_live_readiness_detail_api_returns_gate_report(monkeypatch):
    repo = _repo()
    settings = _live_settings()
    _make_live_ready_at(repo, settings, PASS_CHECKED_AT)

    original_get_detail_report = LiveReadinessService.get_detail_report

    def pinned_get_detail_report(self, *, checked_at=None):
        return original_get_detail_report(self, checked_at=PASS_CHECKED_AT)

    class FakeSession:
        def close(self) -> None:
            pass

    class FakeService:
        def __init__(self) -> None:
            self.repository = repo
            self.settings = settings

        def bootstrap(self) -> dict:
            return {}

    def fake_runtime():
        return FakeSession(), FakeService()

    monkeypatch.setattr(LiveReadinessService, "get_detail_report", pinned_get_detail_report)
    monkeypatch.setattr(admin_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_principal] = lambda: AdminPrincipal(username="viewer", role="VIEWER")
    try:
        response = client.get("/live-readiness/detail")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "PASSED"
    assert payload["checked_at"] == PASS_CHECKED_AT.isoformat()
    assert len(payload["gates"]) == len(GATE_NAMES)
    assert [gate["gate_name"] for gate in payload["gates"]] == GATE_NAMES
    assert all(gate["passed"] for gate in payload["gates"])
