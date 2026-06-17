from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
from sqlalchemy import desc, func, select

from trading_system.app.catalysts.catalyst_engine import CatalystEngine, CatalystRunResult
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import (
    DecisionOutcome,
    DecisionType,
    Direction,
    EnvironmentMode,
    MarketRegime,
    OrderStatus,
    TradeType,
)
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector, AlpacaBarsResult
from trading_system.app.data.collectors.alpaca_stream import (
    AlpacaMarketDataStream,
    AlpacaStreamRunResult,
)
from trading_system.app.data.collectors.alpha_vantage_news import AlphaVantageNewsCollector
from trading_system.app.data.collectors.news_rss import NewsCollectionResult
from trading_system.app.data.collectors.sec_edgar import SecCollectionResult, SecEdgarCollector
from trading_system.app.data.collectors.yahoo_chart import YahooChartCollector, YahooChartResult
from trading_system.app.data.quality_repair import (
    MissingCandleRepairResult,
    MissingCandleRepairService,
)
from trading_system.app.data.universe import LiquidUniverseBuilder, UniverseRefreshResult
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.execution.alpaca_live_adapter import AlpacaLiveAdapter
from trading_system.app.execution.alpaca_paper_adapter import (
    AlpacaPaperAdapter,
    AlpacaPaperOrderResult,
    AlpacaPaperSyncResult,
)
from trading_system.app.execution.fill_reconciliation import (
    FillReconciliationLoop,
    FillReconciliationResult,
)
from trading_system.app.execution.live_execution import LiveExecutionResult, LiveExecutionService
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.execution.order_side import entry_side_from_direction
from trading_system.app.execution.reconciliation import (
    PositionSnapshot,
    ReconciliationResult,
    reconcile_positions,
)
from trading_system.app.signals.idempotency import build_idempotency_key
from trading_system.app.features.calculations import (
    FEATURE_CALCULATION_VERSION,
    LiquidityGates,
    calculate_atr,
    calculate_relative_volume,
    calculate_spread_bps,
    calculate_volume_spike_score,
    calculate_vwap,
)
from trading_system.app.features.production_features import (
    FeatureRunResult,
    ProductionFeatureEngine,
)
from trading_system.app.learning.recommendations import (
    LearningRecommendationEngine,
    LearningRunResult,
)
from trading_system.app.monitoring.trade_monitor_service import (
    TradeMonitorRunResult,
    TradeMonitorService,
)
from trading_system.app.ops.coordination import DistributedLock, redis_client_from_settings
from trading_system.app.ops.provider_health import ProviderHealthRunResult, ProviderHealthService
from trading_system.app.research.vectorbt_backtests import (
    BacktestAssumptions,
    SURVIVORSHIP_BIAS_WARNING,
    run_vwap_reclaim_backtest,
)
from trading_system.app.risk.kill_switch import KillSwitchActionResult, KillSwitchService
from trading_system.app.regime.regime_service import MarketRegimeService, RegimeRunResult
from trading_system.app.risk.live_gates import LiveGateService
from trading_system.app.risk.risk_engine import (
    PortfolioState,
    RiskDecision,
    RiskEngine,
    calculate_annualized_volatility_from_ewma_true_range,
    calculate_ewma_true_range,
)
from trading_system.app.risk.live_readiness import LiveReadinessResult, LiveReadinessService
from trading_system.app.scanners.production_scanners import (
    ProductionScannerEngine,
    ProductionScannerRunResult,
)
from trading_system.app.security.auth import AuthService
from trading_system.app.scanners.vwap_reclaim import VwapReclaimScanner, VwapReclaimSnapshot
from trading_system.app.services.ranking.opportunity_ranking import build_preflight_payload
from trading_system.app.services.replay.decision_snapshot_service import DecisionSnapshotService
from trading_system.app.services.signals.scanner_signal_bridge import ScannerSignalBridgeService
from trading_system.app.services.scheduler import ScheduledCollectorRunner, ScheduledJobResult
from trading_system.app.signals.signal_engine import SignalEngine, TradeSignal
from trading_system.app.strategies.cooldowns import StrategyCooldownBook
from trading_system.app.strategies.registry import StrategyRegistryService


@dataclass(frozen=True)
class ScanCycleResult:
    symbol: str
    collected: AlpacaBarsResult | YahooChartResult | None
    scanner_result_id: str | None
    signal_id: str | None
    thesis_id: str | None
    reason: str


class PortfolioService:
    """Builds server-authoritative portfolio state for risk checks."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def build_state(
        self,
        *,
        signal: TradeSignal,
        environment_mode: str,
        broker: str,
        loss_controls: dict[str, float],
    ) -> PortfolioState:
        snapshot = self.repository.latest_broker_account_snapshot(
            environment_mode=environment_mode,
            broker=broker,
        )
        if not snapshot or snapshot.equity is None or snapshot.equity <= 0:
            raise RuntimeError(
                f"No authoritative broker account equity snapshot available for {environment_mode}/{broker}."
            )
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        open_positions = int(
            self.repository.session.scalar(
                select(func.count(models.Position.id)).where(
                    models.Position.environment_mode == environment_mode,
                    models.Position.quantity != 0,
                )
            )
            or 0
        )
        trades_today = int(
            self.repository.session.scalar(
                select(func.count(models.Order.id)).where(
                    models.Order.environment_mode == environment_mode,
                    models.Order.created_at >= today_start,
                )
            )
            or 0
        )
        strategy_trades_today = int(
            self.repository.session.scalar(
                select(func.count(models.Order.id))
                .join(models.Signal, models.Signal.id == models.Order.signal_id)
                .where(
                    models.Order.environment_mode == environment_mode,
                    models.Signal.strategy_id == signal.strategy_id,
                    models.Order.created_at >= today_start,
                )
            )
            or 0
        )
        return PortfolioState(
            account_equity=float(snapshot.equity),
            open_positions=open_positions,
            daily_loss_pct=loss_controls["daily_loss_pct"],
            weekly_loss_pct=loss_controls["weekly_loss_pct"],
            sector_exposure_pct=0.0,
            trades_today=trades_today,
            trades_by_strategy_today={signal.strategy_id: strategy_trades_today},
        )




class RiskAndSyncOrchestrator:
    """Orchestrates broker sync, reconciliation, cancellations, and flattening."""

    def __init__(
        self,
        repository: TradingRepository,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.strategy_registry = StrategyRegistryService()
        self.cooldowns = StrategyCooldownBook()

    def bootstrap(self) -> dict[str, int]:
        if self.settings.auto_create_schema_enabled:
            self.repository.create_schema()
        self.repository.seed_defaults()
        AuthService(self.repository, self.settings).bootstrap_configured_admin()
        return self.repository.counts()

    async def sync_alpaca_paper(self) -> AlpacaPaperSyncResult:
        adapter = AlpacaPaperAdapter(self.settings)
        result = await adapter.sync()
        self.repository.store_broker_sync(
            environment_mode=self.settings.environment_mode.value,
            broker="alpaca_paper",
            success=result.success,
            mismatch_detected=False,
            reason=result.reason,
            payload={
                "configured": result.configured,
                "account": result.account,
                "positions_count": len(result.positions),
                "orders_count": len(result.orders),
            },
        )
        if result.success:
            self.repository.store_broker_account_snapshot(
                environment_mode=self.settings.environment_mode.value,
                broker="alpaca_paper",
                account=result.account,
                reason="Alpaca paper account synced.",
            )
            for position in result.positions:
                symbol = str(position.get("symbol", "")).upper()
                if not symbol:
                    continue
                qty = float(position.get("qty") or 0.0)
                avg = _float_or_none(position.get("avg_entry_price"))
                self.repository.upsert_position(
                    environment_mode=self.settings.environment_mode.value,
                    symbol=symbol,
                    quantity=qty,
                    average_price=avg,
                    broker_quantity=qty,
                    broker_average_price=avg,
                    reconciliation_status="SYNCED_FROM_ALPACA_PAPER",
                    reason="Position synced from Alpaca paper API.",
                )
        return result

    async def sync_alpaca_live(self) -> dict[str, Any]:
        with DistributedLock(redis_client_from_settings(self.settings), "live_broker_sync_lock"):
            return await self._sync_alpaca_live_locked()

    async def _sync_alpaca_live_locked(self) -> dict[str, Any]:
        if self.settings.environment_mode != EnvironmentMode.LIVE or not (
            self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key
        ):
            blockers = []
            if self.settings.environment_mode != EnvironmentMode.LIVE:
                blockers.append("environment_mode_live")
            if not (self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key):
                blockers.append("live_keys_present")
            reason = "Alpaca live sync blocked before broker call: " + ", ".join(blockers)
            reconciliation = ReconciliationResult(False, reason)
            self.repository.store_broker_sync(
                environment_mode=EnvironmentMode.LIVE.value,
                broker="alpaca_live",
                success=False,
                mismatch_detected=False,
                reason=reason,
                payload={
                    "configured": bool(
                        self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key
                    ),
                    "blocked": True,
                    "blockers": blockers,
                    "reconciliation": reconciliation.__dict__,
                },
            )
            self.repository.store_audit_log(
                actor="system",
                event_type="ALPACA_LIVE_SYNC_BLOCKED",
                entity_type="broker_sync",
                entity_id="alpaca_live",
                reason=reason,
                payload={
                    "environment_mode": self.settings.environment_mode.value,
                    "blockers": blockers,
                },
            )
            return {
                "configured": bool(
                    self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key
                ),
                "success": False,
                "blocked": True,
                "reason": reason,
                "account": None,
                "positions": [],
                "orders": [],
                "reconciliation": reconciliation.__dict__,
            }
        adapter = AlpacaLiveAdapter(self.settings)
        result = await adapter.sync()
        snapshots = []
        if result.success:
            self.repository.store_broker_account_snapshot(
                environment_mode="live",
                broker="alpaca_live",
                account=result.account,
                reason="Alpaca live account synced.",
            )
            for position in result.positions:
                symbol = str(position.get("symbol", "")).upper()
                if not symbol:
                    continue
                broker_qty = float(position.get("qty") or 0.0)
                internal = self.repository.session.scalar(
                    select(models.Position).where(
                        models.Position.environment_mode == "live",
                        models.Position.symbol == symbol,
                    )
                )
                snapshots.append(
                    PositionSnapshot(
                        symbol=symbol,
                        internal_quantity=float(internal.quantity) if internal else 0.0,
                        broker_quantity=broker_qty,
                    )
                )
        reconciliation = reconcile_positions(snapshots)
        self.repository.store_broker_sync(
            environment_mode="live",
            broker="alpaca_live",
            success=result.success and reconciliation.ok,
            mismatch_detected=not reconciliation.ok,
            reason=result.reason if not result.success else reconciliation.reason,
            payload={
                "configured": result.configured,
                "account": result.account,
                "positions_count": len(result.positions),
                "orders_count": len(result.orders),
                "reconciliation": reconciliation.__dict__,
            },
        )
        if result.success and reconciliation.ok:
            for position in result.positions:
                symbol = str(position.get("symbol", "")).upper()
                if not symbol:
                    continue
                qty = float(position.get("qty") or 0.0)
                avg = _float_or_none(position.get("avg_entry_price"))
                self.repository.upsert_position(
                    environment_mode="live",
                    symbol=symbol,
                    quantity=qty,
                    average_price=avg,
                    broker_quantity=qty,
                    broker_average_price=avg,
                    reconciliation_status="SYNCED_FROM_ALPACA_LIVE",
                    reason="Position synced from Alpaca live API.",
                )
        return result.__dict__ | {"reconciliation": reconciliation.__dict__}

    async def cancel_all_live_orders(self, *, actor: str, reason: str) -> dict[str, Any]:
        gate = LiveGateService(self.repository, self.settings).evaluate_operational_action(
            action="live_cancel_all_orders"
        )
        if not gate.allowed:
            result = {
                "success": False,
                "reason": gate.reason,
                "gate_decision": gate.__dict__,
            }
            self.repository.store_execution_error(
                order_id=None,
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="LIVE_CANCEL_ALL_BLOCKED",
                reason=gate.reason,
                payload=gate.__dict__,
            )
        else:
            result = (await AlpacaLiveAdapter(self.settings).cancel_all_orders()).__dict__
            if not result.get("success"):
                self.repository.store_execution_error(
                    order_id=None,
                    environment_mode=EnvironmentMode.LIVE.value,
                    error_type="LIVE_CANCEL_ALL_FAILED",
                    reason=str(result.get("reason") or "Live cancel-all failed."),
                    payload=result.get("payload"),
                )
        self.repository.store_audit_log(
            actor=actor,
            event_type="LIVE_CANCEL_ALL_ORDERS",
            entity_type="execution",
            entity_id=None,
            reason=reason,
            payload=result | {"gate_decision": gate.__dict__},
        )
        return result

    async def flatten_all_live_positions(self, *, actor: str, reason: str) -> dict[str, Any]:
        gate = LiveGateService(self.repository, self.settings).evaluate_operational_action(
            action="live_flatten_all_positions"
        )
        if not gate.allowed:
            result = {
                "success": False,
                "reason": gate.reason,
                "gate_decision": gate.__dict__,
            }
            self.repository.store_execution_error(
                order_id=None,
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="LIVE_FLATTEN_ALL_BLOCKED",
                reason=gate.reason,
                payload=gate.__dict__,
            )
        else:
            result = (await AlpacaLiveAdapter(self.settings).flatten_all_positions()).__dict__
            if not result.get("success"):
                self.repository.store_execution_error(
                    order_id=None,
                    environment_mode=EnvironmentMode.LIVE.value,
                    error_type="LIVE_FLATTEN_ALL_FAILED",
                    reason=str(result.get("reason") or "Live flatten-all failed."),
                    payload=result.get("payload"),
                )
        self.repository.store_audit_log(
            actor=actor,
            event_type="LIVE_FLATTEN_ALL_POSITIONS",
            entity_type="execution",
            entity_id=None,
            reason=reason,
            payload=result | {"gate_decision": gate.__dict__},
        )
        return result

    def run_fill_reconciliation_once(self) -> FillReconciliationResult:
        self.bootstrap()
        return FillReconciliationLoop(self.repository, self.settings).run_once()

    def run_trade_monitor(self) -> TradeMonitorRunResult:
        self.bootstrap()
        return TradeMonitorService(self.repository).run_once()

    def activate_kill_switch(
        self,
        *,
        event_type: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> KillSwitchActionResult:
        self.bootstrap()
        return KillSwitchService(self.repository).activate(
            event_type=event_type,
            reason=reason,
            payload=payload,
            actor=actor,
        )

    def resolve_kill_switch(
        self,
        *,
        event_id: str,
        reason: str,
        actor: str = "system",
    ) -> KillSwitchActionResult:
        self.bootstrap()
        return KillSwitchService(self.repository).resolve(
            event_id=event_id, reason=reason, actor=actor
        )


def _db_signal_to_trade_signal(row: models.Signal) -> TradeSignal:
    return TradeSignal(
        symbol=row.symbol,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        trade_type=TradeType(row.trade_type),
        direction=Direction(row.direction),
        entry_zone=(float(row.entry_zone["low"]), float(row.entry_zone["high"])),
        stop_loss=float(row.stop_loss),
        target_1=float(row.target_1 or 0),
        target_2=float(row.target_2) if row.target_2 is not None else None,
        risk_reward=float(row.risk_reward or 0),
        confidence_score=float(row.confidence_score),
        time_horizon=row.time_horizon or "",
        invalidation=row.invalidation,
        source_timestamp=row.source_timestamp or datetime.now(UTC),
        idempotency_key=row.idempotency_key,
        rule_version=row.signal_rule_version,
    )

def _snapshot_to_payload(snapshot: VwapReclaimSnapshot) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "timestamp": snapshot.timestamp.isoformat(),
        "price": snapshot.price,
        "previous_price": snapshot.previous_price,
        "vwap": snapshot.vwap,
        "previous_vwap": snapshot.previous_vwap,
        "relative_volume": snapshot.relative_volume,
        "average_volume": snapshot.average_volume,
        "dollar_volume": snapshot.dollar_volume,
        "spread_bps": snapshot.spread_bps,
        "market_regime": snapshot.market_regime.value,
        "has_catalyst": snapshot.has_catalyst,
        "strong_relative_strength": snapshot.strong_relative_strength,
    }

def _trade_signal_to_payload(signal: TradeSignal) -> dict[str, Any]:
    return {
        "symbol": signal.symbol,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "trade_type": signal.trade_type.value,
        "direction": signal.direction.value,
        "entry_zone": {"low": signal.entry_zone[0], "high": signal.entry_zone[1]},
        "stop_loss": signal.stop_loss,
        "target_1": signal.target_1,
        "target_2": signal.target_2,
        "risk_reward": signal.risk_reward,
        "confidence_score": signal.confidence_score,
        "time_horizon": signal.time_horizon,
        "invalidation": signal.invalidation,
        "source_timestamp": signal.source_timestamp.isoformat(),
        "idempotency_key": signal.idempotency_key,
        "status": signal.status.value,
        "rule_version": signal.rule_version,
    }

def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
