from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, select

from trading_system.app.core.config import get_settings
from trading_system.app.alpha.expectancy import AlphaExpectancyRefreshService
from trading_system.app.alpha.intelligence import (
    MultiBaggerScoringService,
    OptionsIntelligenceService,
    PointInTimeUniverseService,
    ShortInterestService,
)
from trading_system.app.alpha.leadership import SectorLeadershipService
from trading_system.app.alpha.scoring import AlphaOpportunityScoringService
from trading_system.app.alpha.strategies import ALPHA_STRATEGIES
from trading_system.app.core.enums import AdminRole, EnvironmentMode, MarketRegime
from trading_system.app.data.market_calendar import get_session, opening_range_window
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.db.session import SessionLocal
from trading_system.app.execution.order_manager import OrderManager
from trading_system.app.execution.paper_execution import PaperExecutionEngine
from trading_system.app.execution.reconciliation import (
    PositionSnapshot,
    reconcile_positions,
)
from trading_system.app.features.calculations import LiquidityGates
from trading_system.app.risk.live_readiness import LiveReadinessService
from trading_system.app.risk.risk_engine import PortfolioState, RiskEngine
from trading_system.app.scanners.vwap_reclaim import (
    VwapReclaimScanner,
    VwapReclaimSnapshot,
)
from trading_system.app.services.ranking.expectancy import (
    ExpectancyService,
    latest_market_regime,
    stats_to_dict,
)
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityRankingResult,
    OpportunityRankingService,
)
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.services.streaming_events import redis_event_stream
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
from trading_system.app.tasks import run_alpha_strategy_scanner, run_production_scanners


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
    weekly_loss_pct: float = 0.0
    sector_exposure_pct: float = 0.0
    symbol_exposure_pct: float = 0.0
    strategy_exposure_pct: float = 0.0
    correlated_exposure_pct: float = 0.0
    overnight_exposure_pct: float = 0.0
    event_risk_active: bool = False
    spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    trades_by_strategy_today: dict[str, int] = Field(default_factory=dict)
    kill_switch_active: bool = False
    broker_sync_ok: bool = True
    broker_sync_reason: str = "Broker/internal reconciliation is clean."

    @model_validator(mode="before")
    @classmethod
    def reject_client_authoritative_state(cls, data: Any) -> Any:
        if isinstance(data, dict):
            forbidden = {"account_equity", "open_positions", "daily_loss_pct", "trades_today"}
            supplied = sorted(forbidden.intersection(data))
            if supplied:
                raise ValueError(
                    "Authoritative risk fields are server-derived and must not be supplied: "
                    + ", ".join(supplied)
                )
        return data


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
    collect_first: bool = True


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
    weekly_loss_pct: float = 0.0
    sector_exposure_pct: float = 0.0
    symbol_exposure_pct: float = 0.0
    strategy_exposure_pct: float = 0.0
    correlated_exposure_pct: float = 0.0
    overnight_exposure_pct: float = 0.0
    event_risk_active: bool = False
    spread_bps: float = 0.0
    expected_slippage_bps: float = 0.0
    internal_quantity: float = 0.0
    broker_quantity: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def reject_client_authoritative_state(cls, data: Any) -> Any:
        if isinstance(data, dict):
            forbidden = {"account_equity", "open_positions", "daily_loss_pct", "trades_today"}
            supplied = sorted(forbidden.intersection(data))
            if supplied:
                raise ValueError(
                    "Authoritative risk fields are server-derived and must not be supplied: "
                    + ", ".join(supplied)
                )
        return data


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


class AlphaScannerRunRequest(BaseModel):
    strategy_id: str
    symbols: list[str] | None = None


class AlphaRunRequest(BaseModel):
    symbols: list[str] | None = None
    limit: int = Field(default=100, ge=1, le=500)


class PointInTimeUniverseRunRequest(BaseModel):
    universe_name: str = "tradable_us_equities"
    as_of: datetime | None = None


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


class JournalEntryCreateBody(BaseModel):
    symbol: str
    strategy_id: str | None = None
    entry_thesis: str
    actual_entry: float | None = None
    actual_exit: float | None = None
    pnl: float = 0.0
    human_notes: str | None = None
    mistake_tags: list[str] = Field(default_factory=list)


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






















def _stats_bucket_dict(bucket_stats: dict) -> dict:
    return {bucket: stats_to_dict(stats) for bucket, stats in bucket_stats.items()}


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





class MarketCandlePage(BaseModel):
    candles: list[dict[str, Any]]
    next_cursor: str | None = None
    limit: int




















def _ranking_to_dict(result: OpportunityRankingResult) -> dict:
    return {
        "scanner_result_id": result.scanner_result_id,
        "symbol": result.symbol,
        "strategy_id": result.strategy_id,
        "scanner_name": result.scanner_name,
        "opportunity_score": result.opportunity_score,
        "grade": result.grade.value,
        "reasons": result.reasons,
        "blocked_reason": result.blocked_reason,
        "ranking_rule_version": result.ranking_rule_version,
    }




























































































































async def _submit_db_signal_to_paper(request: DbPaperSubmitRequest) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return await service.submit_signal_to_paper(
            signal_id=request.signal_id,
            weekly_loss_pct=request.weekly_loss_pct,
            sector_exposure_pct=request.sector_exposure_pct,
            symbol_exposure_pct=request.symbol_exposure_pct,
            strategy_exposure_pct=request.strategy_exposure_pct,
            correlated_exposure_pct=request.correlated_exposure_pct,
            overnight_exposure_pct=request.overnight_exposure_pct,
            event_risk_active=request.event_risk_active,
            spread_bps=request.spread_bps,
            expected_slippage_bps=request.expected_slippage_bps,
            internal_quantity=request.internal_quantity,
            broker_quantity=request.broker_quantity,
        )
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
    strategy = strategy_registry.get(decision.strategy_id)
    stop_loss = request.stop_loss or request.vwap
    signal = SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=request.timestamp,
        price=request.price,
        stop_loss=stop_loss,
        strategy_version=strategy.version,
        target_1_rr=strategy.target_1_rr,
        target_2_rr=strategy.target_2_rr,
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







__all__ = [name for name in globals() if not name.startswith("__")]
