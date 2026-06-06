from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import Direction, EnvironmentMode, OrderStatus, ProviderHealthStatus, StrategyStatus, TradeType
from trading_system.app.data.quality_repair import MissingCandleRepairService
from trading_system.app.data.universe import LiquidUniverseBuilder
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.execution.alpaca_live_adapter import (
    AlpacaLiveEmergencyResult,
    AlpacaLiveOrderResult,
    AlpacaLiveSyncResult,
)
from trading_system.app.execution.live_execution import LiveExecutionService
from trading_system.app.execution.reconciliation import ReconciliationResult
from trading_system.app.risk.kill_switch import KillSwitchService
from trading_system.app.risk.live_gates import LiveGateService
from trading_system.app.risk.live_readiness import LiveReadinessService
from trading_system.app.risk.risk_engine import RiskDecision
from trading_system.app.security.auth import AuthService, decode_session_token
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.signals.signal_engine import TradeSignal
from trading_system.app.strategies.approval import StrategyApprovalWorkflow


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


class FakeLiveSubmitAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.configured = True
        self.payload = None
        self.calls = []

    def submit_limit_bracket_order(self, **kwargs) -> AlpacaLiveOrderResult:
        self.payload = kwargs
        self.calls.append(kwargs)
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=True,
            reason="fake live order submitted",
            broker_order_id="broker-live-1",
            payload={"client_order_id": kwargs["client_order_id"]},
        )

    def submit_market_order(self, **kwargs) -> AlpacaLiveOrderResult:
        self.payload = kwargs
        self.calls.append(kwargs)
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=True,
            reason="fake live market order submitted",
            broker_order_id="broker-live-market-1",
            payload={"client_order_id": kwargs["client_order_id"], "type": "market"},
        )


class FakeLiveRejectedAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.configured = True

    def submit_limit_bracket_order(self, **kwargs) -> AlpacaLiveOrderResult:
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=False,
            reason="fake live broker rejected order",
            broker_order_id=None,
            payload={"client_order_id": kwargs["client_order_id"], "error": "rejected"},
        )


class FakeLiveEmergencyFailureAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.configured = True

    def cancel_all_orders(self) -> AlpacaLiveEmergencyResult:
        return AlpacaLiveEmergencyResult(
            configured=True,
            success=False,
            reason="fake cancel-all failed",
            payload={"operation": "cancel_all"},
        )

    def flatten_all_positions(self) -> AlpacaLiveEmergencyResult:
        return AlpacaLiveEmergencyResult(
            configured=True,
            success=False,
            reason="fake flatten-all failed",
            payload={"operation": "flatten_all"},
        )


class FakeLiveSyncAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def sync(self) -> AlpacaLiveSyncResult:
        return AlpacaLiveSyncResult(
            configured=True,
            success=True,
            reason="fake live sync ok",
            account={
                "id": "live-sync-account",
                "status": "ACTIVE",
                "currency": "USD",
                "equity": "125000.50",
                "cash": "40000.25",
                "buying_power": "250000.75",
                "daytrade_count": "1",
                "pattern_day_trader": False,
            },
            positions=[],
            orders=[],
        )


def _live_settings() -> Settings:
    return Settings(
        environment_mode=EnvironmentMode.LIVE,
        allow_live_trading=True,
        confirm_live_trading="I_UNDERSTAND_RISK",
        enable_live_order_path=True,
        alpaca_live_api_key="live-key",
        alpaca_live_secret_key="live-secret",
        admin_session_secret="unit-test-live-session-secret",
    )


def _make_live_ready(repo: TradingRepository, settings: Settings) -> None:
    now = datetime.now(UTC)
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )
    strategy.status = StrategyStatus.APPROVED_SMALL_SIZE.value
    repo.session.commit()
    repo.store_live_trading_approval(
        approved_by="admin",
        reason="test approval",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    for provider in ["alpaca_market_data", "alpaca_live"]:
        repo.store_provider_health_snapshot(
            provider_name=provider,
            status=ProviderHealthStatus.HEALTHY.value,
            reason="test healthy",
            reliability_score=100.0,
            source_timestamp=now,
        )
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.LIVE.value,
        broker="alpaca_live",
        account={
            "id": "live-account-test",
            "status": "ACTIVE",
            "currency": "USD",
            "equity": "100000",
            "cash": "50000",
            "buying_power": "200000",
            "daytrade_count": "0",
            "pattern_day_trader": False,
        },
        reason="test live account snapshot",
        source_timestamp=now,
    )
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": now,
            "raw_payload": {"test": "fresh live-readiness candle"},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": now,
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
    LiveReadinessService(repo, settings).generate_report()


def _store_test_signal(repo: TradingRepository) -> models.Signal:
    signal = models.Signal(
        idempotency_key="live-blocked-signal-1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction="LONG",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Stop loss breaks",
        status="APPROVED",
        signal_rule_version="test",
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(signal)
    repo.session.commit()
    return signal


def test_auth_service_bootstraps_admin_and_authenticates_session():
    repo = _repo()
    settings = Settings(admin_username="admin", admin_password="secret", admin_session_secret="unit-test")
    result = AuthService(repo, settings).login("admin", "secret")

    assert result.authenticated is True
    assert result.token
    assert len(result.token.split(".")) == 3
    token_payload = decode_session_token(result.token, settings)
    assert token_payload is not None
    assert token_payload["username"] == "admin"
    principal = AuthService(repo, settings).authenticate_token(result.token)
    assert principal is not None
    assert principal.username == "admin"
    user = repo.admin_user_by_username("admin")
    assert user is not None
    assert user.password_hash.startswith(("$2a$", "$2b$", "$2y$"))


def test_admin_session_revocation_invalidates_existing_token():
    repo = _repo()
    settings = Settings(admin_username="admin", admin_password="secret", admin_session_secret="unit-test")
    auth = AuthService(repo, settings)
    result = auth.login("admin", "secret")
    user = repo.admin_user_by_username("admin")

    assert result.authenticated is True
    assert result.token
    assert user is not None
    assert auth.authenticate_token(result.token) is not None

    revoked = repo.revoke_admin_sessions_for_user(
        user_id=user.id,
        reason="unit test revocation",
    )

    assert revoked == 1
    assert auth.authenticate_token(result.token) is None


def test_auth_service_locks_user_after_repeated_failed_logins():
    repo = _repo()
    settings = Settings(
        admin_username="admin",
        admin_password="secret",
        admin_session_secret="unit-test",
        admin_failed_login_lockout_attempts=2,
        admin_lockout_minutes=15,
    )
    auth = AuthService(repo, settings)

    first = auth.login("admin", "wrong")
    second = auth.login("admin", "wrong")
    blocked = auth.login("admin", "secret")
    user = repo.admin_user_by_username("admin")

    assert first.authenticated is False
    assert second.authenticated is False
    assert blocked.authenticated is False
    assert "locked until" in blocked.reason
    assert user is not None
    assert user.locked_until is not None
    assert "FAILED_LOGIN_LOCKED" in {row["event_type"] for row in repo.latest_audit_logs(10)}


def test_strategy_approval_requires_evidence_then_updates_status():
    repo = _repo()
    workflow = StrategyApprovalWorkflow(repo)

    missing = workflow.request_status_change(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        requested_status=StrategyStatus.APPROVED_SMALL_SIZE.value,
        requested_by="admin",
        evidence={},
        reason="promote",
    )
    assert missing.accepted is False
    assert "one_step_promotion" in missing.reason

    report = repo.store_backtest_report(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        universe_name="approval-test",
        assumptions={"slippage_bps": 5},
        metrics={"trade_count": 24, "profit_factor": 1.32},
        report_uri="s3://unit-test/backtests/vwap-reclaim.json",
        survivorship_bias_warning="Unit test report only; production reports must use a liquid universe.",
        reason="persisted backtest evidence",
    )
    paper_request = workflow.request_status_change(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        requested_status=StrategyStatus.PAPER_TESTING.value,
        requested_by="admin",
        evidence={
            "strategy_id": "VWAP_RECLAIM",
            "strategy_version": "v1",
            "backtest_report_id": report.id,
        },
        reason="backtest evidence supports paper testing",
    )
    assert paper_request.accepted is True
    paper_decision = workflow.approve_status_change(
        request_id=paper_request.request_id,
        approved=True,
        decided_by="admin",
        decision_reason="backtest evidence reviewed",
    )
    assert paper_decision.approved is True

    request = workflow.request_status_change(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        requested_status=StrategyStatus.APPROVED_SMALL_SIZE.value,
        requested_by="admin",
        evidence={
            "paper_trades": 20,
            "paper_positive_expectancy": True,
            "rule_violations": 0,
            "reconciliation_clean": True,
        },
        reason="paper evidence supports small-size testing",
    )
    assert request.accepted is True

    decision = workflow.approve_status_change(
        request_id=request.request_id,
        approved=True,
        decided_by="admin",
        decision_reason="evidence reviewed",
    )
    assert decision.approved is True
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )
    assert strategy.status == StrategyStatus.APPROVED_SMALL_SIZE.value


def test_strategy_paper_testing_requires_persisted_backtest_report():
    repo = _repo()
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )
    strategy.status = StrategyStatus.RESEARCH.value
    repo.session.commit()
    workflow = StrategyApprovalWorkflow(repo)

    ad_hoc_metrics = workflow.request_status_change(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        requested_status=StrategyStatus.PAPER_TESTING.value,
        requested_by="researcher",
        evidence={
            "strategy_id": "VWAP_RECLAIM",
            "strategy_version": "v1",
            "backtest_metrics": {"trade_count": 12, "profit_factor": 1.4},
        },
        reason="ad hoc metrics are not enough",
    )

    report = repo.store_backtest_report(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        universe_name="unit-test",
        assumptions={"slippage_bps": 5},
        metrics={"trade_count": 12, "profit_factor": 1.4},
        report_uri="s3://unit-test/backtests/vwap.json",
        survivorship_bias_warning="Universe fixed for unit test.",
        reason="Persisted backtest evidence.",
    )
    audit = repo.latest_audit_logs(1)[0]
    persisted = workflow.request_status_change(
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        requested_status=StrategyStatus.PAPER_TESTING.value,
        requested_by="researcher",
        evidence={
            "strategy_id": "VWAP_RECLAIM",
            "strategy_version": "v1",
            "backtest_report_id": report.id,
        },
        reason="persisted backtest supports paper testing",
    )
    decision = workflow.approve_status_change(
        request_id=persisted.request_id,
        approved=True,
        decided_by="admin",
        decision_reason="backtest evidence reviewed",
    )

    assert ad_hoc_metrics.accepted is False
    assert "persisted_backtest_report" in ad_hoc_metrics.reason
    assert audit["event_type"] == "BACKTEST_REPORT_STORED"
    assert audit["entity_id"] == report.id
    assert audit["payload"]["metrics"]["trade_count"] == 12
    assert persisted.accepted is True
    assert decision.approved is True
    assert strategy.status == StrategyStatus.PAPER_TESTING.value


def test_live_readiness_and_gates_can_pass_only_when_all_controls_are_green():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)

    report = repo.latest_live_readiness_reports(1)[0]
    assert report["live_allowed"] is True
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")
    assert gate.allowed is True


def test_live_readiness_report_audit_links_actor_and_report_id():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)

    result = LiveReadinessService(repo, settings).generate_report(actor="ops-admin")
    audit = repo.latest_audit_logs(1)[0]

    assert audit["event_type"] == "LIVE_READINESS_REPORT"
    assert audit["actor"] == "ops-admin"
    assert audit["entity_id"] == result.report_id
    assert audit["payload"]["live_allowed"] is True


def test_runtime_bootstrap_preserves_strategy_approval_state():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)

    TradingRuntimeService(repo, settings=settings).bootstrap()
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )

    assert strategy.status == StrategyStatus.APPROVED_SMALL_SIZE.value
    assert gate.allowed is True


def test_missing_live_account_snapshot_blocks_readiness_and_gate():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    repo.session.query(models.BrokerAccountSnapshot).delete()
    repo.session.commit()

    readiness = LiveReadinessService(repo, settings).generate_report()
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")

    assert readiness.live_allowed is False
    assert gate.allowed is False
    assert "live_account_snapshot_usable" in gate.blockers
    report = repo.latest_live_readiness_reports(1)[0]
    check = next(item for item in report["checks"] if item["check_name"] == "live_account_snapshot_usable")
    assert check["passed"] is False


def test_expired_live_approval_is_marked_expired_and_audited():
    repo = _repo()
    settings = _live_settings()
    expired = repo.store_live_trading_approval(
        approved_by="admin",
        reason="expired test approval",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    active = repo.active_live_trading_approval()
    row = repo.session.get(models.LiveTradingApproval, expired.id)
    audit = repo.latest_audit_logs(1)[0]

    assert active is None
    assert row.status == "EXPIRED"
    assert row.revoked_at is not None
    assert audit["event_type"] == "LIVE_APPROVAL_EXPIRED"
    assert audit["entity_id"] == expired.id


def test_expired_live_approval_blocks_readiness_check():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    approval = repo.active_live_trading_approval()
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    repo.session.commit()

    readiness = LiveReadinessService(repo, settings).generate_report()
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")
    report = repo.latest_live_readiness_reports(1)[0]
    check = next(item for item in report["checks"] if item["check_name"] == "active_human_live_approval")

    assert readiness.live_allowed is False
    assert gate.allowed is False
    assert "active_human_approval" in gate.blockers
    assert check["passed"] is False
    assert repo.session.get(models.LiveTradingApproval, approval.id).status == "EXPIRED"


def test_mocked_live_allowed_flow_uses_separate_live_adapter_after_all_gates_pass():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    signal_row = _store_test_signal(repo)
    trade_signal = TradeSignal(
        symbol=signal_row.symbol,
        strategy_id=signal_row.strategy_id,
        strategy_version=signal_row.strategy_version,
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(100.0, 101.0),
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Stop loss breaks",
        source_timestamp=signal_row.source_timestamp,
        idempotency_key=signal_row.idempotency_key,
        rule_version=signal_row.signal_rule_version,
    )
    adapter = FakeLiveSubmitAdapter(settings)

    result = LiveExecutionService(repo, adapter=adapter).submit_limit_order(
        signal=trade_signal,
        signal_id=signal_row.id,
        risk_decision=RiskDecision(
            approved=True,
            reason="risk approved",
            risk_rule_version="test",
            position_size=10,
            risk_amount=50.0,
        ),
        reconciliation=ReconciliationResult(ok=True, reason="clean reconciliation"),
    )

    order = repo.latest_orders(1)[0]
    assert result.accepted is True
    assert result.broker_submit["submitted"] is True
    assert adapter.payload["client_order_id"] == order["idempotency_key"]
    assert order["execution_environment"] == "LIVE"
    assert order["broker_order_id"] == "broker-live-1"


def test_duplicate_live_submit_is_rejected_before_second_broker_call():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    signal_row = _store_test_signal(repo)
    trade_signal = TradeSignal(
        symbol=signal_row.symbol,
        strategy_id=signal_row.strategy_id,
        strategy_version=signal_row.strategy_version,
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(100.0, 101.0),
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Stop loss breaks",
        source_timestamp=signal_row.source_timestamp,
        idempotency_key=signal_row.idempotency_key,
        rule_version=signal_row.signal_rule_version,
    )
    adapter = FakeLiveSubmitAdapter(settings)
    service = LiveExecutionService(repo, adapter=adapter)
    risk_decision = RiskDecision(
        approved=True,
        reason="risk approved",
        risk_rule_version="test",
        position_size=10,
        risk_amount=50.0,
    )

    first = service.submit_limit_order(
        signal=trade_signal,
        signal_id=signal_row.id,
        risk_decision=risk_decision,
        reconciliation=ReconciliationResult(ok=True, reason="clean reconciliation"),
    )
    second = service.submit_limit_order(
        signal=trade_signal,
        signal_id=signal_row.id,
        risk_decision=risk_decision,
        reconciliation=ReconciliationResult(ok=True, reason="clean reconciliation"),
    )

    error = repo.latest_execution_errors(1)[0]

    assert first.accepted is True
    assert second.accepted is False
    assert "Duplicate live order" in second.reason
    assert len(adapter.calls) == 1
    assert repo.counts()["orders"] == 1
    assert error["order_id"] == first.order["id"]
    assert error["error_type"] == "DUPLICATE_LIVE_ORDER"


def test_live_broker_submit_failure_rejects_order_and_persists_execution_error():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    signal_row = _store_test_signal(repo)
    trade_signal = TradeSignal(
        symbol=signal_row.symbol,
        strategy_id=signal_row.strategy_id,
        strategy_version=signal_row.strategy_version,
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(100.0, 101.0),
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Stop loss breaks",
        source_timestamp=signal_row.source_timestamp,
        idempotency_key=signal_row.idempotency_key,
        rule_version=signal_row.signal_rule_version,
    )

    result = LiveExecutionService(repo, adapter=FakeLiveRejectedAdapter(settings)).submit_limit_order(
        signal=trade_signal,
        signal_id=signal_row.id,
        risk_decision=RiskDecision(
            approved=True,
            reason="risk approved",
            risk_rule_version="test",
            position_size=10,
            risk_amount=50.0,
        ),
        reconciliation=ReconciliationResult(ok=True, reason="clean reconciliation"),
    )

    order = repo.latest_orders(1)[0]
    error = repo.latest_execution_errors(1)[0]
    decision = repo.latest_decisions(1)[0]
    snapshot = TradingRuntimeService(repo, settings=settings).dashboard_snapshot()

    assert result.accepted is False
    assert result.broker_submit["submitted"] is False
    assert order["status"] == "REJECTED"
    assert error["order_id"] == order["id"]
    assert error["error_type"] == "LIVE_BROKER_SUBMIT_FAILED"
    assert error["reason"] == "fake live broker rejected order"
    assert decision["outcome"] == "BLOCKED"
    assert snapshot["counts"]["execution_errors"] == 1
    assert snapshot["execution_errors"][0]["id"] == error["id"]


def test_internal_live_market_order_is_blocked_until_live_gates_pass():
    repo = _repo()
    signal_row = _store_test_signal(repo)
    order = models.Order(
        signal_id=signal_row.id,
        idempotency_key="protective-exit-live-blocked-test",
        environment_mode=EnvironmentMode.LIVE.value,
        execution_environment="LIVE",
        broker="alpaca_live",
        broker_order_id=None,
        symbol="AMD",
        side="sell",
        quantity=10,
        order_type="market",
        limit_price=None,
        stop_loss=None,
        status=OrderStatus.SUBMITTED.value,
        expected_price=97.5,
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(order)
    repo.session.commit()

    result = TradingRuntimeService(repo, settings=Settings(environment_mode=EnvironmentMode.LIVE_DISABLED)).submit_internal_order_to_broker(
        order_id=order.id,
        actor="unit-test",
        reason="submit live protective exit",
    )

    assert result["accepted"] is False
    assert "environment_mode_live" in result["gate_decision"]["blockers"]
    assert repo.latest_broker_sync_logs(1) == []


def test_internal_live_market_order_submits_after_all_live_gates_pass(monkeypatch):
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    signal_row = _store_test_signal(repo)
    order = models.Order(
        signal_id=signal_row.id,
        idempotency_key="protective-exit-live-allowed-test",
        environment_mode=EnvironmentMode.LIVE.value,
        execution_environment="LIVE",
        broker="alpaca_live",
        broker_order_id=None,
        symbol="AMD",
        side="sell",
        quantity=10,
        order_type="market",
        limit_price=None,
        stop_loss=None,
        status=OrderStatus.SUBMITTED.value,
        expected_price=97.5,
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(order)
    repo.session.commit()
    monkeypatch.setattr(
        "trading_system.app.services.runtime.AlpacaLiveAdapter",
        FakeLiveSubmitAdapter,
    )

    result = TradingRuntimeService(repo, settings=settings).submit_internal_order_to_broker(
        order_id=order.id,
        actor="unit-test",
        reason="submit live protective exit",
    )

    updated = repo.latest_orders(1)[0]

    assert result["accepted"] is True
    assert result["broker_submit"]["submitted"] is True
    assert updated["broker_order_id"] == "broker-live-market-1"
    assert repo.latest_broker_sync_logs(1)[0]["success"] is True


def test_default_admin_session_secret_blocks_live_readiness():
    repo = _repo()
    settings = Settings(
        environment_mode=EnvironmentMode.LIVE,
        allow_live_trading=True,
        confirm_live_trading="I_UNDERSTAND_RISK",
        enable_live_order_path=True,
        alpaca_live_api_key="live-key",
        alpaca_live_secret_key="live-secret",
        admin_session_secret="change-me",
    )
    _make_live_ready(repo, settings)

    report = repo.latest_live_readiness_reports(1)[0]
    secret_check = next(
        check for check in report["checks"] if check["check_name"] == "admin_session_secret_configured"
    )

    assert report["live_allowed"] is False
    assert secret_check["passed"] is False


def test_stale_provider_health_blocks_live_readiness_and_gate():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    old = datetime.now(UTC) - timedelta(seconds=settings.provider_health_max_age_seconds + 30)
    for provider in ["alpaca_market_data", "alpaca_live"]:
        repo.store_provider_health_snapshot(
            provider_name=provider,
            status=ProviderHealthStatus.HEALTHY.value,
            reason="stale test healthy",
            reliability_score=100.0,
            source_timestamp=old,
        )

    readiness = LiveReadinessService(repo, settings).generate_report()
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")

    assert readiness.live_allowed is False
    assert gate.allowed is False
    assert "alpaca_market_data_healthy" in gate.blockers
    assert "alpaca_live_healthy" in gate.blockers


def test_stale_live_account_snapshot_blocks_live_readiness_and_gate():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    old = datetime.now(UTC) - timedelta(seconds=settings.provider_health_max_age_seconds + 30)
    repo.store_broker_account_snapshot(
        environment_mode=EnvironmentMode.LIVE.value,
        broker="alpaca_live",
        account={
            "id": "stale-live-account",
            "status": "ACTIVE",
            "currency": "USD",
            "equity": "100000",
            "cash": "50000",
            "buying_power": "200000",
        },
        reason="stale test live account",
        source_timestamp=old,
    )

    readiness = LiveReadinessService(repo, settings).generate_report()
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")

    assert readiness.live_allowed is False
    assert gate.allowed is False
    assert "live_account_snapshot_usable" in gate.blockers


def test_kill_switch_blocks_live_gate_after_readiness_passed():
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)

    KillSwitchService(repo).activate(event_type="TEST", reason="unit test", payload={})
    gate = LiveGateService(repo, settings).evaluate(strategy_id="VWAP_RECLAIM", signal_id="sig-test")

    assert gate.allowed is False
    assert "no_active_kill_switch" in gate.blockers


def test_live_submit_is_testable_but_records_blocked_order_by_default():
    repo = _repo()
    signal = _store_test_signal(repo)
    settings = Settings(environment_mode=EnvironmentMode.LIVE_DISABLED)
    service = TradingRuntimeService(repo, settings=settings)

    result = service.submit_signal_to_live(
        signal_id=signal.id,
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
        internal_quantity=0.0,
        broker_quantity=0.0,
    )

    order = repo.latest_orders(1)[0]
    assert result.accepted is False
    assert "environment_mode_live" in result.gate_decision["blockers"]
    assert "live_order_path_enabled" in result.gate_decision["blockers"]
    assert order["environment_mode"] == EnvironmentMode.LIVE.value
    assert order["execution_environment"] == "LIVE_DISABLED"
    assert order["status"] == "REJECTED"
    assert repo.latest_decisions(1)[0]["outcome"] == "BLOCKED"


def test_live_emergency_actions_are_blocked_and_audited_by_default():
    repo = _repo()
    service = TradingRuntimeService(repo, settings=Settings(environment_mode=EnvironmentMode.LIVE_DISABLED))

    cancel = service.cancel_all_live_orders(actor="admin", reason="unit test cancel")
    flatten = service.flatten_all_live_positions(actor="admin", reason="unit test flatten")
    audit_events = {row["event_type"] for row in repo.latest_audit_logs(10)}
    errors = repo.latest_execution_errors(5)

    assert cancel["success"] is False
    assert flatten["success"] is False
    assert "blocked" in cancel["reason"]
    assert "blocked" in flatten["reason"]
    assert "environment_mode_live" in cancel["gate_decision"]["blockers"]
    assert "latest_readiness_passed" in flatten["gate_decision"]["blockers"]
    assert {row["error_type"] for row in errors} == {
        "LIVE_CANCEL_ALL_BLOCKED",
        "LIVE_FLATTEN_ALL_BLOCKED",
    }
    assert {"LIVE_CANCEL_ALL_ORDERS", "LIVE_FLATTEN_ALL_POSITIONS"}.issubset(audit_events)


def test_alpaca_live_sync_persists_account_snapshot(monkeypatch):
    repo = _repo()
    settings = _live_settings()
    service = TradingRuntimeService(repo, settings=settings)
    monkeypatch.setattr(
        "trading_system.app.services.runtime.AlpacaLiveAdapter",
        FakeLiveSyncAdapter,
    )

    result = service.sync_alpaca_live()
    account = repo.latest_broker_account_snapshot(
        environment_mode=EnvironmentMode.LIVE.value,
        broker="alpaca_live",
    )

    assert result["success"] is True
    assert account is not None
    assert account.account_id == "live-sync-account"
    assert account.equity == 125000.50
    assert account.buying_power == 250000.75
    assert repo.counts()["broker_account_snapshots"] == 1


def test_alpaca_live_sync_is_blocked_by_default_before_adapter_call(monkeypatch):
    repo = _repo()
    service = TradingRuntimeService(repo, settings=Settings(environment_mode=EnvironmentMode.LIVE_DISABLED))

    class ShouldNotBeCalled:
        def __init__(self, _settings):
            raise AssertionError("live adapter should not be constructed when sync is blocked")

    monkeypatch.setattr(
        "trading_system.app.services.runtime.AlpacaLiveAdapter",
        ShouldNotBeCalled,
    )

    result = service.sync_alpaca_live()
    sync_log = repo.latest_broker_sync_logs(1)[0]
    audit = repo.latest_audit_logs(1)[0]

    assert result["success"] is False
    assert result["blocked"] is True
    assert "environment_mode_live" in result["reason"]
    assert sync_log["success"] is False
    assert sync_log["payload"]["blocked"] is True
    assert audit["event_type"] == "ALPACA_LIVE_SYNC_BLOCKED"


def test_live_emergency_adapter_failures_are_persisted_as_execution_errors(monkeypatch):
    repo = _repo()
    settings = _live_settings()
    _make_live_ready(repo, settings)
    service = TradingRuntimeService(repo, settings=settings)
    monkeypatch.setattr(
        "trading_system.app.services.runtime.AlpacaLiveAdapter",
        FakeLiveEmergencyFailureAdapter,
    )

    cancel = service.cancel_all_live_orders(actor="admin", reason="unit test cancel")
    flatten = service.flatten_all_live_positions(actor="admin", reason="unit test flatten")
    errors = repo.latest_execution_errors(5)
    audits = repo.latest_audit_logs(5)

    assert cancel["success"] is False
    assert flatten["success"] is False
    assert {row["error_type"] for row in errors} == {
        "LIVE_CANCEL_ALL_FAILED",
        "LIVE_FLATTEN_ALL_FAILED",
    }
    audit_events = {row["event_type"] for row in audits}
    assert "LIVE_CANCEL_ALL_ORDERS" in audit_events
    assert "LIVE_FLATTEN_ALL_POSITIONS" in audit_events


def test_missing_candle_repair_records_gap_and_universe_builder_blocks_illiquid_symbol():
    repo = _repo()
    start = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    for idx, ts in enumerate([start, start + timedelta(minutes=5)]):
        raw_id = repo.store_raw_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": 2.0,
                "high": 2.1,
                "low": 1.9,
                "close": 2.0,
                "volume": 1000,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": "test",
            }
        )

    repair = MissingCandleRepairService(repo, Settings()).run_once(["AMD"])
    universe = LiquidUniverseBuilder(repo, Settings()).refresh(["AMD"])

    assert repair.gaps_detected == 1
    assert repo.latest_missing_candle_gaps(1)[0]["repaired"] is False
    assert universe.disabled_or_blocked == 1
    symbol = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "AMD"))
    assert symbol.is_tradable is False


def test_universe_builder_rejects_research_only_yahoo_rows_for_production_tradability():
    repo = _repo()
    now = datetime.now(UTC)
    for idx in range(5):
        ts = now - timedelta(minutes=idx)
        raw_id = repo.store_raw_candle(
            {
                "provider": "yahoo_chart",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "yahoo_chart",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": 50.0,
                "high": 50.02,
                "low": 49.98,
                "close": 50.0,
                "volume": 2_000_000,
                "trade_count": None,
                "vwap": 50.0,
                "data_quality_status": "VALID",
                "quality_reason": "research-only test",
            }
        )

    universe = LiquidUniverseBuilder(repo, Settings(bar_freshness_max_seconds=600)).refresh(["AMD"])
    symbol = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "AMD"))

    assert universe.disabled_or_blocked == 1
    assert symbol.is_tradable is False
    assert symbol.tradability_reason == "No clean Alpaca market data available for production liquidity gates."


def test_universe_builder_accepts_fresh_liquid_alpaca_rows():
    repo = _repo()
    now = datetime.now(UTC)
    for idx in range(20):
        ts = now - timedelta(minutes=idx)
        raw_id = repo.store_raw_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "alpaca_market_data",
                "symbol": "AMD",
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": 50.0,
                "high": 50.02,
                "low": 49.98,
                "close": 50.0,
                "volume": 2_000_000,
                "trade_count": None,
                "vwap": 50.0,
                "data_quality_status": "VALID",
                "quality_reason": "fresh liquid test",
            }
        )

    universe = LiquidUniverseBuilder(repo, Settings(bar_freshness_max_seconds=600)).refresh(["AMD"])
    symbol = repo.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == "AMD"))

    assert universe.tradable == 1
    assert symbol.is_tradable is True
    assert symbol.tradability_reason == "Symbol passes configured liquidity gates."
