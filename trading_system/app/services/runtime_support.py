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

# Orchestrator modules intentionally import this support module with ``import *``
# so the physically extracted methods retain access to their former module-level
# helper functions, including private helper names.
__all__ = [name for name in globals() if not name.startswith("__")]
