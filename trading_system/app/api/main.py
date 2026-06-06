from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from trading_system.app.core.config import get_settings
from trading_system.app.core.enums import AdminRole, EnvironmentMode, MarketRegime
from trading_system.app.data.market_calendar import get_session, opening_range_window
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.db.session import SessionLocal
from trading_system.app.execution.order_manager import OrderManager
from trading_system.app.execution.paper_execution import PaperExecutionEngine
from trading_system.app.execution.reconciliation import PositionSnapshot, reconcile_positions
from trading_system.app.features.calculations import LiquidityGates
from trading_system.app.risk.risk_engine import PortfolioState, RiskEngine
from trading_system.app.scanners.vwap_reclaim import VwapReclaimScanner, VwapReclaimSnapshot
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.security.auth import (
    AdminPrincipal,
    AuthService,
    hash_password,
    require_admin_token,
    require_principal,
    require_trader_or_admin,
)
from trading_system.app.signals.signal_engine import SignalEngine
from trading_system.app.strategies.cooldowns import StrategyCooldownBook
from trading_system.app.strategies.registry import StrategyRegistryService


app = FastAPI(title="Autonomous Trading Intelligence Platform", version="0.1.0")

strategy_registry = StrategyRegistryService()
cooldowns = StrategyCooldownBook()


class VwapReclaimScanRequest(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    previous_price: float
    vwap: float
    previous_vwap: float
    relative_volume: float
    average_volume: float
    dollar_volume: float
    spread_bps: float
    market_regime: MarketRegime = MarketRegime.CHOPPY
    has_catalyst: bool = False
    strong_relative_strength: bool = False
    stop_loss: float | None = None


class RiskCheckRequest(BaseModel):
    account_equity: float = Field(gt=0)
    open_positions: int = 0
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0
    sector_exposure_pct: float = 0.0
    symbol_exposure_pct: float = 0.0
    strategy_exposure_pct: float = 0.0
    correlated_exposure_pct: float = 0.0
    overnight_exposure_pct: float = 0.0
    event_risk_active: bool = False
    spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    trades_today: int = 0
    trades_by_strategy_today: dict[str, int] = Field(default_factory=dict)
    kill_switch_active: bool = False
    broker_sync_ok: bool = True
    broker_sync_reason: str = "Broker/internal reconciliation is clean."


class PaperOrderRequest(BaseModel):
    scan: VwapReclaimScanRequest
    risk: RiskCheckRequest
    internal_quantity: float = 0.0
    broker_quantity: float = 0.0


class SymbolRequest(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    reason: str = "Activated through API."


class SymbolDeactivateRequest(BaseModel):
    symbol: str
    reason: str


class SymbolTradabilityRequest(BaseModel):
    symbol: str
    is_tradable: bool
    reason: str


class CollectRequest(BaseModel):
    symbols: list[str] | None = None


class AlpacaBarsCollectRequest(BaseModel):
    symbols: list[str] | None = None
    timeframe: str | None = None


class AlpacaStreamRequest(BaseModel):
    symbols: list[str] | None = None
    channels: list[str] | None = None
    max_messages: int = Field(default=25, ge=1, le=500)


class SecCollectRequest(BaseModel):
    symbols: list[str] | None = None
    max_filings_per_symbol: int = Field(default=10, ge=1, le=100)


class SchedulerRunRequest(BaseModel):
    job_name: str
    symbols: list[str] | None = None


class DbPaperSubmitRequest(BaseModel):
    signal_id: str
    account_equity: float = Field(gt=0)
    open_positions: int = 0
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0
    sector_exposure_pct: float = 0.0
    symbol_exposure_pct: float = 0.0
    strategy_exposure_pct: float = 0.0
    correlated_exposure_pct: float = 0.0
    overnight_exposure_pct: float = 0.0
    event_risk_active: bool = False
    spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    trades_today: int = 0
    strategy_trades_today: int = 0
    internal_quantity: float = 0.0
    broker_quantity: float = 0.0


class AuthLoginRequest(BaseModel):
    username: str
    password: str


class AdminUserUpsertBody(BaseModel):
    username: str
    password: str = Field(min_length=12)
    role: AdminRole = AdminRole.VIEWER
    reason: str


class AdminUserRoleBody(BaseModel):
    username: str
    role: AdminRole
    reason: str


class AdminUserActiveBody(BaseModel):
    username: str
    is_active: bool
    reason: str


class AdminUserUnlockBody(BaseModel):
    username: str
    reason: str


class StrategyStatusChangeBody(BaseModel):
    requested_status: str
    strategy_version: str = "v1"
    evidence: dict = Field(default_factory=dict)
    reason: str


class StrategyStatusDecisionBody(BaseModel):
    request_id: str
    approved: bool
    decision_reason: str


class BacktestRunRequest(BaseModel):
    strategy_id: str = "VWAP_RECLAIM"
    symbol: str = "SPY"
    provider: str = "alpaca_market_data"


class KillSwitchActivateBody(BaseModel):
    event_type: str
    reason: str
    payload: dict | None = None


class KillSwitchResolveBody(BaseModel):
    event_id: str
    reason: str


class LiveEmergencyBody(BaseModel):
    reason: str


class LiveApprovalBody(BaseModel):
    reason: str
    expires_at: datetime | None = None


class LiveApprovalRevokeBody(BaseModel):
    approval_id: str
    reason: str


class OrderReplaceBody(BaseModel):
    order_id: str
    reason: str
    new_limit_price: float | None = None
    new_stop_loss: float | None = None


class OrderBrokerSubmitBody(BaseModel):
    order_id: str
    reason: str = "Submit internal OMS order to broker."


def _runtime() -> tuple[SessionLocal, TradingRuntimeService]:
    session = SessionLocal()
    repo = TradingRepository(session)
    service = TradingRuntimeService(repo)
    return session, service


def _store_symbol_config_audit(
    repo: TradingRepository,
    *,
    actor: str,
    event_type: str,
    row: models.SymbolUniverse,
    reason: str,
    payload: dict | None = None,
) -> None:
    repo.store_audit_log(
        actor=actor,
        event_type=event_type,
        entity_type="symbol_universe",
        entity_id=row.id,
        reason=reason,
        payload={
            "symbol": row.symbol,
            "is_active": row.is_active,
            "is_tradable": row.is_tradable,
            "sector": row.sector,
            **(payload or {}),
        },
    )


def _result_summary(result: Any) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    if not isinstance(raw, dict):
        return {"result_type": type(result).__name__}
    summary_keys = [
        "success",
        "reason",
        "overall_status",
        "live_allowed",
        "report_id",
        "blockers",
        "warnings",
        "job_name",
        "result_count",
        "version",
    ]
    summary = {key: raw[key] for key in summary_keys if key in raw}
    summary["result_type"] = type(result).__name__
    return summary


def _audit_manual_operation(
    repo: TradingRepository,
    *,
    actor: str,
    operation: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    result: Any = None,
) -> None:
    repo.store_audit_log(
        actor=actor,
        event_type="MANUAL_OPERATION_RUN",
        entity_type="manual_operation",
        entity_id=operation,
        reason=reason,
        payload={
            "operation": operation,
            "request": payload or {},
            "result": _result_summary(result) if result is not None else {},
        },
    )


@app.post("/auth/login")
def auth_login(request: AuthLoginRequest) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = AuthService(service.repository, service.settings).login(request.username, request.password)
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)
        return {
            "token": result.token,
            "username": result.username,
            "role": result.role,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "reason": result.reason,
        }
    finally:
        session.close()


@app.post("/auth/logout")
def auth_logout(
    principal: AdminPrincipal = Depends(require_principal),
    authorization: str | None = Header(default=None),
) -> dict:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    session, service = _runtime()
    try:
        service.bootstrap()
        revoked = AuthService(service.repository, service.settings).logout(token, actor=principal.username)
        return {"revoked": revoked, "reason": "Logout processed."}
    finally:
        session.close()


@app.get("/admin/users")
def admin_users(
    _principal: AdminPrincipal = Depends(require_admin_token),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"admin_users": service.repository.list_admin_users(limit)}
    finally:
        session.close()


@app.post("/admin/users")
def admin_user_upsert(
    request: AdminUserUpsertBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required.")
    if username == principal.username and request.role != AdminRole.ADMIN:
        raise HTTPException(status_code=409, detail="Admins cannot demote their own active session user.")
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.upsert_admin_user(
            username=username,
            password_hash=hash_password(request.password),
            role=request.role.value,
            reason=request.reason,
        )
        revoked_sessions = service.repository.revoke_admin_sessions_for_user(
            user_id=row.id,
            reason="Admin user password or role updated; active sessions revoked.",
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_UPSERTED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={
                "username": row.username,
                "role": row.role,
                "is_active": row.is_active,
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"admin_user": _admin_user_response(row)}
    finally:
        session.close()


@app.post("/admin/users/role")
def admin_user_role(
    request: AdminUserRoleBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    username = request.username.strip()
    if username == principal.username and request.role != AdminRole.ADMIN:
        raise HTTPException(status_code=409, detail="Admins cannot demote their own active session user.")
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_admin_user_role(
            username=username,
            role=request.role.value,
            reason=request.reason,
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_ROLE_CHANGED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={"username": row.username, "role": row.role},
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/admin/users/active")
def admin_user_active(
    request: AdminUserActiveBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    if request.username.strip() == principal.username and not request.is_active:
        raise HTTPException(status_code=409, detail="Admins cannot deactivate their own active session user.")
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_admin_user_active(
            username=request.username.strip(),
            is_active=request.is_active,
            reason=request.reason,
        )
        revoked_sessions = 0
        if not row.is_active:
            revoked_sessions = service.repository.revoke_admin_sessions_for_user(
                user_id=row.id,
                reason="Admin user deactivated; active sessions revoked.",
            )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_ACTIVE_CHANGED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={
                "username": row.username,
                "is_active": row.is_active,
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/admin/users/unlock")
def admin_user_unlock(
    request: AdminUserUnlockBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.clear_admin_user_lockout(
            username=request.username.strip(),
            reason=request.reason,
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_UNLOCKED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={"username": row.username},
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


def _admin_user_response(row) -> dict:
    return {
        "id": row.id,
        "username": row.username,
        "role": row.role,
        "is_active": row.is_active,
        "failed_login_count": row.failed_login_count,
        "locked_until": row.locked_until,
        "last_login_at": row.last_login_at,
        "reason": row.reason,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "source_timestamp": row.source_timestamp,
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "message": "Production-gated platform. Live trading path is disabled unless every live gate passes.",
    }


@app.get("/ops/health")
def ops_health(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    settings = get_settings()
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "status": "ok",
            "environment_mode": settings.environment_mode.value,
            "live_order_path_enabled": settings.live_order_path_enabled,
            "counts": service.repository.counts(),
            "active_kill_switches": service.repository.active_kill_switch_count(),
        }
    finally:
        session.close()


@app.get("/ops/workers")
def ops_workers(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"workers": service.repository.latest_worker_heartbeats(100)}
    finally:
        session.close()


@app.get("/environment")
def environment(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    settings = get_settings()
    return {
        "mode": settings.environment_mode.value,
        "allowed_modes": [item.value for item in EnvironmentMode],
        "default_mode": EnvironmentMode.RESEARCH.value,
        "live_default": False,
        "live_order_path_enabled": settings.live_order_path_enabled,
    }


@app.get("/provider-capabilities")
def provider_capabilities(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"provider_capabilities": service.repository.list_rows(models.ProviderCapability, 100)}
    finally:
        session.close()


@app.get("/calendar/session")
def calendar_session(timestamp: datetime) -> dict:
    info = get_session(timestamp)
    return {
        "status": info.status.value,
        "session_date": info.session_date.isoformat(),
        "open_at": info.open_at.isoformat() if info.open_at else None,
        "close_at": info.close_at.isoformat() if info.close_at else None,
        "reason": info.reason,
    }


@app.get("/calendar/opening-range")
def calendar_opening_range(session_date: datetime, minutes: int = 15) -> dict:
    start, end = opening_range_window(session_date.date(), minutes=minutes)
    return {"start": start.isoformat(), "end": end.isoformat(), "minutes": minutes}


@app.get("/strategies")
def strategies(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        rows = service.repository.list_rows(models.StrategyRegistry, 100)
        for row in rows:
            row["paper_trade_allowed"] = row["status"] in {
                "PAPER_TESTING",
                "APPROVED_SMALL_SIZE",
                "APPROVED_FULL_SIZE",
            }
            row["live_trade_allowed"] = row["status"] in {
                "APPROVED_SMALL_SIZE",
                "APPROVED_FULL_SIZE",
            }
        return {"strategies": rows}
    finally:
        session.close()


@app.post("/db/bootstrap")
def bootstrap_database(
    _principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return {"counts": service.bootstrap()}
    finally:
        session.close()


@app.get("/dashboard/snapshot")
def dashboard_snapshot(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        return service.dashboard_snapshot()
    finally:
        session.close()


def _read_rows(
    key: str,
    reader: Callable[[TradingRepository, int], list[dict]],
    limit: int,
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {key: reader(service.repository, limit)}
    finally:
        session.close()


@app.get("/universe")
def universe(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"active_symbols": service.repository.active_symbols()}
    finally:
        session.close()


@app.get("/market/clean-candles")
def market_clean_candles(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("clean_candles", lambda repo, row_limit: repo.latest_clean_candles(row_limit), limit)


@app.get("/features/latest")
def latest_features(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("features", lambda repo, row_limit: repo.latest_features(row_limit), limit)


@app.get("/features/daily")
def daily_features(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("daily_features", lambda repo, row_limit: repo.latest_daily_features(row_limit), limit)


@app.get("/regime/snapshots")
def regime_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("regime_snapshots", lambda repo, row_limit: repo.latest_regime_snapshots(row_limit), limit)


@app.get("/catalysts/events")
def catalyst_events(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("events", lambda repo, row_limit: repo.latest_events(row_limit), limit)


@app.get("/catalysts/scores")
def catalyst_scores(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("catalysts", lambda repo, row_limit: repo.latest_catalysts(row_limit), limit)


@app.get("/scanners/results")
def scanner_results(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("scanner_results", lambda repo, row_limit: repo.latest_scanner_results(row_limit), limit)


@app.get("/signals")
def signals(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("signals", lambda repo, row_limit: repo.latest_signals(row_limit), limit)


@app.get("/signals/theses")
def trade_theses(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("trade_theses", lambda repo, row_limit: repo.latest_trade_theses(row_limit), limit)


@app.get("/risk/checks")
def risk_checks(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("risk_checks", lambda repo, row_limit: repo.latest_risk_checks(row_limit), limit)


@app.get("/risk/exposures")
def exposure_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("exposure_snapshots", lambda repo, row_limit: repo.latest_exposure_snapshots(row_limit), limit)


@app.get("/broker/account-snapshots")
def broker_account_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "broker_account_snapshots",
        lambda repo, row_limit: repo.latest_broker_account_snapshots(row_limit),
        limit,
    )


@app.get("/broker/sync-logs")
def broker_sync_logs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("broker_sync_logs", lambda repo, row_limit: repo.latest_broker_sync_logs(row_limit), limit)


@app.get("/providers/health")
def provider_health(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("provider_health", lambda repo, row_limit: repo.latest_provider_health(row_limit), limit)


@app.get("/providers/rate-limits")
def provider_rate_limits(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("provider_rate_limits", lambda repo, row_limit: repo.latest_provider_rate_limits(row_limit), limit)


@app.get("/streams/events")
def stream_events(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("stream_events", lambda repo, row_limit: repo.latest_stream_events(row_limit), limit)


@app.get("/scheduler/runs")
def scheduler_runs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("scheduler_runs", lambda repo, row_limit: repo.latest_scheduler_runs(row_limit), limit)


@app.get("/data/news")
def clean_news(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("clean_news", lambda repo, row_limit: repo.latest_clean_news(row_limit), limit)


@app.get("/data/sec-filings")
def sec_filings(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("filings", lambda repo, row_limit: repo.latest_filings(row_limit), limit)


@app.get("/data/quality-errors")
def data_quality_errors(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("data_quality_errors", lambda repo, row_limit: repo.latest_data_quality_errors(row_limit), limit)


@app.get("/data/missing-candle-gaps")
def missing_candle_gaps(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("missing_candle_gaps", lambda repo, row_limit: repo.latest_missing_candle_gaps(row_limit), limit)


@app.get("/execution/orders")
def execution_orders(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("orders", lambda repo, row_limit: repo.latest_orders(row_limit), limit)


@app.get("/execution/fills")
def execution_fills(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("fills", lambda repo, row_limit: repo.latest_fills(row_limit), limit)


@app.get("/execution/positions")
def execution_positions(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("positions", lambda repo, row_limit: repo.latest_positions(row_limit), limit)


@app.get("/execution/errors")
def execution_errors(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("execution_errors", lambda repo, row_limit: repo.latest_execution_errors(row_limit), limit)


@app.get("/journal/entries")
def journal_entries(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("journal", lambda repo, row_limit: repo.latest_journal(row_limit), limit)


@app.get("/reviews/trades")
def trade_reviews(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("ai_reviews", lambda repo, row_limit: repo.latest_ai_reviews(row_limit), limit)


@app.get("/reviews/weekly")
def weekly_reviews(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return _read_rows("weekly_reviews", lambda repo, row_limit: repo.latest_weekly_reviews(row_limit), limit)


@app.get("/learning/recommendations")
def learning_recommendations(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "strategy_recommendations",
        lambda repo, row_limit: repo.latest_strategy_recommendations(row_limit),
        limit,
    )


@app.get("/backtests/reports")
def backtest_reports(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    return _read_rows("backtest_reports", lambda repo, row_limit: repo.latest_backtest_reports(row_limit), limit)


@app.get("/strategy-approvals/requests")
def strategy_approval_requests(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "strategy_approval_requests",
        lambda repo, row_limit: repo.latest_strategy_approval_requests(row_limit),
        limit,
    )


@app.get("/kill-switches")
def kill_switches(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows("kill_switches", lambda repo, row_limit: repo.latest_kill_switches(row_limit), limit)


@app.get("/decisions")
def decisions(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    return _read_rows("decisions", lambda repo, row_limit: repo.latest_decisions(row_limit), limit)


@app.get("/audit/logs")
def audit_logs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    return _read_rows("audit_logs", lambda repo, row_limit: repo.latest_audit_logs(row_limit), limit)


@app.post("/symbols/activate")
def activate_symbol(
    request: SymbolRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.add_or_activate_symbol(
            request.symbol,
            name=request.name,
            sector=request.sector,
            reason=request.reason,
        )
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_ACTIVATED",
            row=row,
            reason=request.reason,
            payload={"source": "api", "name": request.name},
        )
        return {"symbol": row.symbol, "is_active": row.is_active, "reason": row.change_reason}
    finally:
        session.close()


@app.post("/symbols/deactivate")
def deactivate_symbol(
    request: SymbolDeactivateRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.deactivate_symbol(request.symbol, reason=request.reason)
        if not row:
            raise HTTPException(status_code=404, detail="Symbol not found.")
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_DEACTIVATED",
            row=row,
            reason=request.reason,
            payload={"source": "api"},
        )
        return {"symbol": row.symbol, "is_active": row.is_active, "reason": row.change_reason}
    finally:
        session.close()


@app.post("/symbols/tradability")
def update_symbol_tradability(
    request: SymbolTradabilityRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_symbol_tradability(
            request.symbol,
            is_tradable=request.is_tradable,
            reason=request.reason,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Symbol not found.")
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_TRADABILITY_CHANGED",
            row=row,
            reason=request.reason,
            payload={"source": "api"},
        )
        return {
            "symbol": row.symbol,
            "is_tradable": row.is_tradable,
            "tradability_reason": row.tradability_reason,
        }
    finally:
        session.close()


@app.post("/universe/refresh")
def universe_refresh(
    request: CollectRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.refresh_universe(request.symbols).__dict__
    finally:
        session.close()


@app.post("/data/repair-missing-candles")
def repair_missing_candles(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.repair_missing_candles(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="repair_missing_candles",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/backtests/run")
def backtests_run(
    request: BacktestRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_backtest(
            strategy_id=request.strategy_id,
            symbol=request.symbol,
            provider=request.provider,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="backtests_run",
            reason="Manual backtest run requested.",
            payload={
                "strategy_id": request.strategy_id,
                "symbol": request.symbol,
                "provider": request.provider,
            },
            result=result,
        )
        return result
    finally:
        session.close()


@app.post("/strategies/{strategy_id}/request-status-change")
def request_strategy_status_change(
    strategy_id: str,
    request: StrategyStatusChangeBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.request_strategy_status_change(
            strategy_id=strategy_id,
            strategy_version=request.strategy_version,
            requested_status=request.requested_status,
            requested_by=principal.username,
            evidence=request.evidence,
            reason=request.reason,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/strategies/{strategy_id}/approve-status-change")
def approve_strategy_status_change(
    strategy_id: str,
    request: StrategyStatusDecisionBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        result = service.approve_strategy_status_change(
            request_id=request.request_id,
            approved=request.approved,
            decided_by=principal.username,
            decision_reason=request.decision_reason,
        )
        if result.request and result.request["strategy_id"] != strategy_id:
            raise HTTPException(status_code=409, detail="Request does not belong to strategy path.")
        return result.__dict__
    finally:
        session.close()


@app.post("/collect/yahoo")
def collect_yahoo(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        symbols = request.symbols or service.repository.active_symbols()
        results = []
        for symbol in symbols:
            row = service.repository.add_or_activate_symbol(symbol, reason="Activated for API collection.")
            if request.symbols:
                _store_symbol_config_audit(
                    service.repository,
                    actor=principal.username,
                    event_type="SYMBOL_ACTIVATED_FOR_COLLECTION",
                    row=row,
                    reason="Activated for API collection.",
                    payload={"source": "api", "collector": "yahoo"},
                )
            results.append(service.collect_symbol(symbol).__dict__)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_yahoo",
            reason="Manual Yahoo research collection requested.",
            payload={"symbols": symbols},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": results}
    finally:
        session.close()


@app.post("/collect/alpaca-bars")
def collect_alpaca_bars(
    request: AlpacaBarsCollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        symbols = request.symbols or service.repository.active_symbols()
        results = []
        from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector

        collector = AlpacaBarsCollector(service.repository, service.settings)
        for symbol in symbols:
            row = service.repository.add_or_activate_symbol(
                symbol,
                reason="Activated for Alpaca bar collection.",
            )
            if request.symbols:
                _store_symbol_config_audit(
                    service.repository,
                    actor=principal.username,
                    event_type="SYMBOL_ACTIVATED_FOR_COLLECTION",
                    row=row,
                    reason="Activated for Alpaca bar collection.",
                    payload={"source": "api", "collector": "alpaca_bars"},
                )
            results.append(collector.collect(symbol, timeframe=request.timeframe).__dict__)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_alpaca_bars",
            reason="Manual Alpaca bar collection requested.",
            payload={"symbols": symbols, "timeframe": request.timeframe},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": results}
    finally:
        session.close()


@app.post("/scan/watchlist")
def scan_watchlist(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        for symbol in request.symbols or []:
            row = service.repository.add_or_activate_symbol(symbol, reason="Activated for API scan.")
            _store_symbol_config_audit(
                service.repository,
                actor=principal.username,
                event_type="SYMBOL_ACTIVATED_FOR_SCAN",
                row=row,
                reason="Activated for API scan.",
                payload={"source": "api"},
            )
        results = service.run_watchlist_scan(collect_first=True)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="scan_watchlist",
            reason="Manual watchlist scan requested.",
            payload={"symbols": request.symbols or []},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": [item.__dict__ for item in results]}
    finally:
        session.close()


def _submit_db_signal_to_paper(request: DbPaperSubmitRequest) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return service.submit_signal_to_paper(
            signal_id=request.signal_id,
            account_equity=request.account_equity,
            open_positions=request.open_positions,
            daily_loss_pct=request.daily_loss_pct,
            weekly_loss_pct=request.weekly_loss_pct,
            sector_exposure_pct=request.sector_exposure_pct,
            symbol_exposure_pct=request.symbol_exposure_pct,
            strategy_exposure_pct=request.strategy_exposure_pct,
            correlated_exposure_pct=request.correlated_exposure_pct,
            overnight_exposure_pct=request.overnight_exposure_pct,
            event_risk_active=request.event_risk_active,
            spread_bps=request.spread_bps,
            expected_slippage_bps=request.expected_slippage_bps,
            trades_today=request.trades_today,
            strategy_trades_today=request.strategy_trades_today,
            internal_quantity=request.internal_quantity,
            broker_quantity=request.broker_quantity,
        )
    finally:
        session.close()


@app.post("/execution/paper/submit-signal")
def submit_db_signal_to_paper(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _submit_db_signal_to_paper(request)


@app.post("/execution/paper/submit")
def submit_paper_signal(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _submit_db_signal_to_paper(request)


@app.post("/execution/live/submit")
def submit_live_signal(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.submit_signal_to_live(
            signal_id=request.signal_id,
            account_equity=request.account_equity,
            open_positions=request.open_positions,
            daily_loss_pct=request.daily_loss_pct,
            weekly_loss_pct=request.weekly_loss_pct,
            sector_exposure_pct=request.sector_exposure_pct,
            symbol_exposure_pct=request.symbol_exposure_pct,
            strategy_exposure_pct=request.strategy_exposure_pct,
            correlated_exposure_pct=request.correlated_exposure_pct,
            overnight_exposure_pct=request.overnight_exposure_pct,
            event_risk_active=request.event_risk_active,
            spread_bps=request.spread_bps,
            expected_slippage_bps=request.expected_slippage_bps,
            trades_today=request.trades_today,
            strategy_trades_today=request.strategy_trades_today,
            internal_quantity=request.internal_quantity,
            broker_quantity=request.broker_quantity,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/execution/live/cancel-all")
def cancel_all_live_orders(
    request: LiveEmergencyBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.cancel_all_live_orders(actor=principal.username, reason=request.reason)
    finally:
        session.close()


@app.post("/execution/live/flatten-all")
def flatten_all_live_positions(
    request: LiveEmergencyBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.flatten_all_live_positions(actor=principal.username, reason=request.reason)
    finally:
        session.close()


@app.post("/execution/orders/replace")
def replace_order(
    request: OrderReplaceBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return OrderManager(service.repository).request_replace_order(
            order_id=request.order_id,
            reason=request.reason,
            actor=principal.username,
            new_limit_price=request.new_limit_price,
            new_stop_loss=request.new_stop_loss,
        ).__dict__
    finally:
        session.close()


@app.post("/execution/orders/submit-broker")
def submit_internal_order_to_broker(
    request: OrderBrokerSubmitBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.submit_internal_order_to_broker(
            order_id=request.order_id,
            actor=principal.username,
            reason=request.reason,
        )
    finally:
        session.close()


@app.post("/broker/alpaca-paper/sync")
def sync_alpaca_paper(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.sync_alpaca_paper()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="sync_alpaca_paper",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/broker/alpaca-live/sync")
def sync_alpaca_live(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.sync_alpaca_live()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="sync_alpaca_live",
            reason=result.get("reason", "Manual Alpaca live sync requested."),
            result=result,
        )
        return result
    finally:
        session.close()


@app.post("/streams/alpaca/market-data/run-once")
async def run_alpaca_market_data_stream(
    request: AlpacaStreamRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = await service.run_alpaca_market_data_stream(
            symbols=request.symbols,
            channels=request.channels,
            max_messages=request.max_messages,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_alpaca_market_data_stream",
            reason=result.reason,
            payload={
                "symbols": request.symbols,
                "channels": request.channels,
                "max_messages": request.max_messages,
            },
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/reconciliation/fills/run-once")
def run_fill_reconciliation(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.run_fill_reconciliation_once()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_fill_reconciliation",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/collect/news")
def collect_news(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.collect_news(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_news",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/collect/sec")
def collect_sec(
    request: SecCollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.collect_sec_filings(
            request.symbols,
            max_filings_per_symbol=request.max_filings_per_symbol,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_sec",
            reason=result.reason,
            payload={
                "symbols": request.symbols,
                "max_filings_per_symbol": request.max_filings_per_symbol,
            },
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/scheduler/run-once")
def run_scheduler_once(
    request: SchedulerRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.run_scheduled_job(
            request.job_name,
            symbols=request.symbols,
            actor=principal.username,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_scheduled_job",
            reason=result.reason,
            payload={"job_name": request.job_name, "symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


def _live_readiness_report(*, actor: str) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.generate_live_readiness_report(actor=actor)
        _audit_manual_operation(
            service.repository,
            actor=actor,
            operation="generate_live_readiness_report",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/live-readiness/report")
def live_readiness_report(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _live_readiness_report(actor=principal.username)


@app.post("/live-readiness/run")
def live_readiness_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _live_readiness_report(actor=principal.username)


@app.get("/live-readiness/reports")
def live_readiness_reports(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"live_readiness_reports": service.repository.latest_live_readiness_reports(limit)}
    finally:
        session.close()


@app.get("/live-readiness/approvals")
def live_readiness_approvals(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"live_trading_approvals": service.repository.latest_live_trading_approvals(limit)}
    finally:
        session.close()


@app.post("/live-readiness/approve")
def live_readiness_approve(
    request: LiveApprovalBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.store_live_trading_approval(
            approved_by=principal.username,
            reason=request.reason,
            expires_at=request.expires_at,
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="LIVE_APPROVAL_CREATED",
            entity_type="live_trading_approval",
            entity_id=row.id,
            reason=request.reason,
            payload={"expires_at": request.expires_at.isoformat() if request.expires_at else None},
        )
        return {"approval": service.repository.latest_live_trading_approvals(1)[0]}
    finally:
        session.close()


@app.post("/live-readiness/approvals/revoke")
def live_readiness_approval_revoke(
    request: LiveApprovalRevokeBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        service.repository.revoke_live_trading_approval(
            approval_id=request.approval_id,
            revoked_by=principal.username,
            reason=request.reason,
        )
        return {"approval": service.repository.latest_live_trading_approvals(1)[0]}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/kill-switches/activate")
def kill_switch_activate(
    request: KillSwitchActivateBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.activate_kill_switch(
            event_type=request.event_type,
            reason=request.reason,
            payload=request.payload,
            actor=principal.username,
        ).__dict__
    finally:
        session.close()


@app.post("/kill-switches/resolve")
def kill_switch_resolve(
    request: KillSwitchResolveBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.resolve_kill_switch(
            event_id=request.event_id,
            reason=request.reason,
            actor=principal.username,
        ).__dict__
    finally:
        session.close()


@app.post("/provider-health/run")
def provider_health_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_provider_health()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="provider_health_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/features/run")
def features_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_features(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="features_run",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/regime/snapshot/run")
def regime_snapshot_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_market_regime()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="regime_snapshot_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/catalysts/score/run")
def catalysts_score_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_catalysts(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="catalysts_score_run",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/scanners/production/run")
def production_scanners_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_production_scanners(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="production_scanners_run",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/monitor/trades/run-once")
def trade_monitor_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_trade_monitor()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="trade_monitor_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/reviews/trades/run")
def trade_reviews_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_reviews()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="trade_reviews_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@app.post("/reviews/weekly/run")
def weekly_reviews_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_learning_review()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="weekly_reviews_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


def _scan_to_signal(request: VwapReclaimScanRequest):
    settings = get_settings()
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(
            min_price=settings.min_price,
            min_average_volume=settings.min_average_volume,
            min_dollar_volume=settings.min_dollar_volume,
            max_spread_bps=settings.max_spread_bps,
        ),
        strategy_registry=strategy_registry,
        cooldowns=cooldowns,
    )
    decision = scanner.scan(
        VwapReclaimSnapshot(
            symbol=request.symbol,
            timestamp=request.timestamp,
            price=request.price,
            previous_price=request.previous_price,
            vwap=request.vwap,
            previous_vwap=request.previous_vwap,
            relative_volume=request.relative_volume,
            average_volume=request.average_volume,
            dollar_volume=request.dollar_volume,
            spread_bps=request.spread_bps,
            market_regime=request.market_regime,
            has_catalyst=request.has_catalyst,
            strong_relative_strength=request.strong_relative_strength,
        )
    )
    if not decision.accepted:
        return decision, None
    stop_loss = request.stop_loss or request.vwap
    signal = SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=request.timestamp,
        price=request.price,
        stop_loss=stop_loss,
    )
    return decision, signal


def _persist_direct_scan_decision(
    repo: TradingRepository,
    *,
    request: VwapReclaimScanRequest,
    decision,
    signal,
    source: str = "api:/scanners/vwap-reclaim",
) -> tuple[str, str | None]:
    scanner = repo.store_generic_scanner_result(
        scanner_name="VWAP_RECLAIM_DIRECT_API",
        scanner_rule_version=decision.rule_version,
        symbol=decision.symbol,
        strategy_id=decision.strategy_id,
        accepted=decision.accepted,
        score=decision.score,
        reason=decision.reason,
        payload={
            "request": jsonable_encoder(request),
            "signal_created": signal is not None,
            "source": source,
        },
        source_timestamp=request.timestamp,
    )
    signal_row = repo.store_signal(signal) if signal else None
    return scanner.id, signal_row.id if signal_row else None


def _persist_direct_risk_decision(
    repo: TradingRepository,
    *,
    request: PaperOrderRequest,
    signal,
    risk_decision,
    signal_id: str | None = None,
    source: str = "api:/risk/check-vwap-reclaim",
) -> str:
    resolved_signal_id = signal_id or repo.store_signal(signal).id
    risk = repo.store_risk_check(
        risk_decision,
        signal_id=resolved_signal_id,
        strategy_id=signal.strategy_id,
        source_timestamp=request.scan.timestamp,
        payload={
            "request": jsonable_encoder(request),
            "source": source,
        },
    )
    return risk.id


@app.post("/scanners/vwap-reclaim")
def scan_vwap_reclaim(
    request: VwapReclaimScanRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request)
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request,
            decision=decision,
            signal=signal,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__ if signal else None,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
    }


@app.post("/risk/check-vwap-reclaim")
def risk_check_vwap_reclaim(
    request: PaperOrderRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request.scan)
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request.scan,
            decision=decision,
            signal=signal,
        )
        if not signal:
            return {
                "scanner_decision": decision.__dict__,
                "risk_decision": None,
                "scanner_result_id": scanner_result_id,
                "signal_id": signal_id,
            }
    finally:
        if not signal:
            session.close()
    if not signal:
        return {"scanner_decision": decision.__dict__, "risk_decision": None}
    risk_decision = RiskEngine().evaluate(
        signal,
        PortfolioState(
            account_equity=request.risk.account_equity,
            open_positions=request.risk.open_positions,
            daily_loss_pct=request.risk.daily_loss_pct,
            weekly_loss_pct=request.risk.weekly_loss_pct,
            sector_exposure_pct=request.risk.sector_exposure_pct,
            symbol_exposure_pct=request.risk.symbol_exposure_pct,
            strategy_exposure_pct=request.risk.strategy_exposure_pct,
            correlated_exposure_pct=request.risk.correlated_exposure_pct,
            overnight_exposure_pct=request.risk.overnight_exposure_pct,
            event_risk_active=request.risk.event_risk_active,
            spread_bps=request.risk.spread_bps,
            expected_slippage_bps=request.risk.expected_slippage_bps,
            trades_today=request.risk.trades_today,
            trades_by_strategy_today=request.risk.trades_by_strategy_today,
            kill_switch_active=request.risk.kill_switch_active,
            broker_sync_ok=request.risk.broker_sync_ok,
            broker_sync_reason=request.risk.broker_sync_reason,
        ),
    )
    try:
        risk_check_id = _persist_direct_risk_decision(
            service.repository,
            request=request,
            signal=signal,
            risk_decision=risk_decision,
            signal_id=signal_id,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__,
        "risk_decision": risk_decision.__dict__,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
        "risk_check_id": risk_check_id,
    }


@app.post("/execution/paper/submit-vwap-reclaim")
def submit_paper_order(
    request: PaperOrderRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request.scan)
    if not signal:
        raise HTTPException(status_code=422, detail=decision.reason)
    risk_decision = RiskEngine().evaluate(
        signal,
        PortfolioState(
            account_equity=request.risk.account_equity,
            open_positions=request.risk.open_positions,
            daily_loss_pct=request.risk.daily_loss_pct,
            weekly_loss_pct=request.risk.weekly_loss_pct,
            sector_exposure_pct=request.risk.sector_exposure_pct,
            symbol_exposure_pct=request.risk.symbol_exposure_pct,
            strategy_exposure_pct=request.risk.strategy_exposure_pct,
            correlated_exposure_pct=request.risk.correlated_exposure_pct,
            overnight_exposure_pct=request.risk.overnight_exposure_pct,
            event_risk_active=request.risk.event_risk_active,
            spread_bps=request.risk.spread_bps,
            expected_slippage_bps=request.risk.expected_slippage_bps,
            trades_today=request.risk.trades_today,
            trades_by_strategy_today=request.risk.trades_by_strategy_today,
            kill_switch_active=request.risk.kill_switch_active,
            broker_sync_ok=request.risk.broker_sync_ok,
            broker_sync_reason=request.risk.broker_sync_reason,
        ),
    )
    reconciliation = reconcile_positions(
        [
            PositionSnapshot(
                symbol=request.scan.symbol,
                internal_quantity=request.internal_quantity,
                broker_quantity=request.broker_quantity,
            )
        ]
    )
    order = PaperExecutionEngine().submit_limit_order(
        signal=signal,
        risk_decision=risk_decision,
        reconciliation=reconciliation,
    )
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request.scan,
            decision=decision,
            signal=signal,
            source="api:/execution/paper/submit-vwap-reclaim",
        )
        risk_check_id = _persist_direct_risk_decision(
            service.repository,
            request=request,
            signal=signal,
            risk_decision=risk_decision,
            signal_id=signal_id,
            source="api:/execution/paper/submit-vwap-reclaim",
        )
        order_row = service.repository.store_order(
            order,
            signal_id=signal_id,
            strategy_id=signal.strategy_id,
            environment_mode=EnvironmentMode.PAPER.value,
            source_timestamp=order.created_at,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__,
        "risk_decision": risk_decision.__dict__,
        "reconciliation": reconciliation.__dict__,
        "order": order.__dict__,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
        "risk_check_id": risk_check_id,
        "order_id": order_row.id,
    }
