from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import desc, func, select

from trading_system.app.ai.thesis_engine import build_rule_based_thesis
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
from trading_system.app.execution.paper_execution import PaperExecutionEngine
from trading_system.app.execution.reconciliation import (
    PositionSnapshot,
    ReconciliationResult,
    reconcile_positions,
)
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
from trading_system.app.journal.review_engine import ReviewRunResult, TradeReviewEngine
from trading_system.app.learning.recommendations import (
    LearningRecommendationEngine,
    LearningRunResult,
)
from trading_system.app.monitoring.trade_monitor_service import (
    TradeMonitorRunResult,
    TradeMonitorService,
)
from trading_system.app.ops.provider_health import ProviderHealthRunResult, ProviderHealthService
from trading_system.app.research.vectorbt_backtests import (
    BacktestAssumptions,
    SURVIVORSHIP_BIAS_WARNING,
    run_vwap_reclaim_backtest,
)
from trading_system.app.risk.kill_switch import KillSwitchActionResult, KillSwitchService
from trading_system.app.regime.regime_service import MarketRegimeService, RegimeRunResult
from trading_system.app.risk.live_gates import LiveGateService
from trading_system.app.risk.risk_engine import PortfolioState, RiskDecision, RiskEngine
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
from trading_system.app.strategies.approval import (
    StrategyApprovalWorkflow,
    StrategyStatusDecisionResult,
    StrategyStatusRequestResult,
)
from trading_system.app.strategies.cooldowns import StrategyCooldownBook
from trading_system.app.strategies.registry import StrategyRegistryService


@dataclass(frozen=True)
class ScanCycleResult:
    symbol: str
    collected: YahooChartResult | None
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


class TradingRuntimeService:
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

    def collect_symbol(self, symbol: str) -> YahooChartResult:
        collector = YahooChartCollector(self.repository)
        return collector.collect(symbol)

    def collect_symbol_primary(self, symbol: str) -> AlpacaBarsResult | YahooChartResult:
        collector = AlpacaBarsCollector(self.repository, self.settings)
        result = collector.collect(symbol)
        if result.success or self.settings.environment_mode != EnvironmentMode.RESEARCH:
            return result
        return self.collect_symbol(symbol)

    def collect_active_symbols(self) -> list[YahooChartResult]:
        self.bootstrap()
        return [self.collect_symbol(symbol) for symbol in self.repository.active_symbols()]

    def run_watchlist_scan(self, *, collect_first: bool = True) -> list[ScanCycleResult]:
        self.bootstrap()
        results: list[ScanCycleResult] = []
        for symbol in self.repository.active_symbols():
            collected = self.collect_symbol(symbol) if collect_first else None
            results.append(self.run_vwap_scan(symbol, collected=collected))
        return results

    def run_vwap_scan(
        self,
        symbol: str,
        *,
        collected: YahooChartResult | None = None,
    ) -> ScanCycleResult:
        normalized = symbol.strip().upper()
        frame = self.repository.clean_candles_df(normalized, limit=500)
        if len(frame) < 2:
            self.repository.store_decision_log(
                decision_type=DecisionType.SCANNER,
                outcome=DecisionOutcome.REJECTED,
                entity_type="symbol",
                entity_id=normalized,
                strategy_id="VWAP_RECLAIM",
                rule_version="vwap_reclaim_scanner_v1",
                reason="Not enough valid clean candles to scan.",
                payload={"rows": len(frame)},
            )
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=None,
                signal_id=None,
                thesis_id=None,
                reason="Not enough valid clean candles to scan.",
            )

        snapshot, feature_payload = self._build_vwap_snapshot(normalized, frame)
        self.repository.store_intraday_features(
            symbol=normalized,
            source_timestamp=snapshot.timestamp,
            feature_version=FEATURE_CALCULATION_VERSION,
            price=snapshot.price,
            vwap=snapshot.vwap,
            atr=feature_payload.get("atr"),
            relative_volume=snapshot.relative_volume,
            gap_pct=None,
            volume_spike_score=feature_payload.get("volume_spike_score"),
            liquidity_score=feature_payload.get("liquidity_score"),
            spread_score=feature_payload.get("spread_score"),
        )
        self.repository.store_feature_snapshot(
            symbol=normalized,
            source_timestamp=snapshot.timestamp,
            feature_version=FEATURE_CALCULATION_VERSION,
            snapshot=feature_payload,
        )

        scanner = VwapReclaimScanner(
            liquidity_gates=LiquidityGates(
                min_price=self.settings.min_price,
                min_average_volume=self.settings.min_average_volume,
                min_dollar_volume=self.settings.min_dollar_volume,
                max_spread_bps=self.settings.max_spread_bps,
            ),
            strategy_registry=self.strategy_registry,
            cooldowns=self.cooldowns,
        )
        decision = scanner.scan(snapshot)
        # Always persist a preflight payload (plus latest close/VWAP) alongside the
        # scanner result. This is additive to the existing payload shape and lets the
        # opportunity-ranking engine score every accepted scanner result, regardless of
        # whether the ranking-gated signal path is enabled.
        preflight = build_preflight_payload(
            self.repository,
            symbol=normalized,
            strategy_id="VWAP_RECLAIM",
            timeframe="1Min",
            latest_data_timestamp=snapshot.timestamp,
        )
        scanner_row = self.repository.store_scanner_result(
            decision,
            source_timestamp=snapshot.timestamp,
            payload={
                "snapshot": _snapshot_to_payload(snapshot),
                "features": feature_payload,
                "spread_note": "Yahoo chart has no bid/ask; spread_bps uses current candle range proxy.",
                "preflight": preflight,
                "latest_close": snapshot.price,
                "latest_vwap": snapshot.vwap,
            },
        )

        if not decision.accepted:
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=scanner_row.id,
                signal_id=None,
                thesis_id=None,
                reason=decision.reason,
            )

        if self.settings.enable_ranking_signal_path:
            return self._create_signal_via_ranking(
                normalized,
                scanner_row,
                snapshot,
                decision,
                collected,
            )

        stop_loss = min(snapshot.vwap, snapshot.price * 0.995)
        signal = SignalEngine().create_vwap_reclaim_signal(
            scanner_decision=decision,
            source_timestamp=snapshot.timestamp,
            price=snapshot.price,
            stop_loss=stop_loss,
        )
        signal_row = self.repository.store_signal(signal)
        self.repository.store_signal_version(
            signal_id=signal_row.id,
            version=signal.rule_version,
            change_reason="Initial signal generated from VWAP reclaim scan.",
            payload=_trade_signal_to_payload(signal),
            source_timestamp=snapshot.timestamp,
        )
        thesis = build_rule_based_thesis(
            symbol=normalized,
            setup_name="VWAP_RECLAIM",
            scanner_reason=decision.reason,
            catalyst_summary=None,
            market_context=f"Market regime input: {snapshot.market_regime.value}",
        )
        thesis_row = self.repository.store_trade_thesis(
            thesis,
            signal_id=signal_row.id,
            symbol=normalized,
            strategy_id=signal.strategy_id,
            source_timestamp=snapshot.timestamp,
        )
        return ScanCycleResult(
            symbol=normalized,
            collected=collected,
            scanner_result_id=scanner_row.id,
            signal_id=signal_row.id,
            thesis_id=thesis_row.id,
            reason="Signal and thesis generated.",
        )

    def _create_signal_via_ranking(
        self,
        normalized: str,
        scanner_row: models.ScannerResult,
        snapshot: VwapReclaimSnapshot,
        decision: Any,
        collected: YahooChartResult | None,
    ) -> ScanCycleResult:
        """Route an accepted scanner result through the opportunity-ranking gate.

        A signal (and thesis) is only created when the ranking engine grades the
        candidate highly enough for the bridge to accept it. Otherwise the scan
        returns with the bridge's blocked reason and no signal.
        """
        bridge = ScannerSignalBridgeService(self.repository, self.settings)
        bridge_result = bridge.try_create_signal(scanner_row.id, now=snapshot.timestamp)
        if not bridge_result.created or bridge_result.signal_id is None:
            return ScanCycleResult(
                symbol=normalized,
                collected=collected,
                scanner_result_id=scanner_row.id,
                signal_id=None,
                thesis_id=None,
                reason=bridge_result.blocked_reason or "Ranking gate did not produce a signal.",
            )

        thesis = build_rule_based_thesis(
            symbol=normalized,
            setup_name="VWAP_RECLAIM",
            scanner_reason=decision.reason,
            catalyst_summary=None,
            market_context=f"Market regime input: {snapshot.market_regime.value}",
        )
        thesis_row = self.repository.store_trade_thesis(
            thesis,
            signal_id=bridge_result.signal_id,
            symbol=normalized,
            strategy_id=scanner_row.strategy_id or "VWAP_RECLAIM",
            source_timestamp=snapshot.timestamp,
        )
        return ScanCycleResult(
            symbol=normalized,
            collected=collected,
            scanner_result_id=scanner_row.id,
            signal_id=bridge_result.signal_id,
            thesis_id=thesis_row.id,
            reason="Ranked signal and thesis generated.",
        )

    def submit_signal_to_paper(
        self,
        *,
        signal_id: str,
        weekly_loss_pct: float = 0.0,
        sector_exposure_pct: float = 0.0,
        symbol_exposure_pct: float = 0.0,
        strategy_exposure_pct: float = 0.0,
        correlated_exposure_pct: float = 0.0,
        overnight_exposure_pct: float = 0.0,
        event_risk_active: bool = False,
        spread_bps: float = 0.0,
        expected_slippage_bps: float = 0.0,
        internal_quantity: float = 0.0,
        broker_quantity: float = 0.0,
    ) -> dict[str, Any]:
        signal_row = self.repository.signal_by_id(signal_id)
        if not signal_row:
            raise ValueError(f"Unknown signal id: {signal_id}")
        signal = _db_signal_to_trade_signal(signal_row)
        authoritative_state = self._authoritative_portfolio_state(
            signal=signal,
            environment_mode=EnvironmentMode.PAPER.value,
            broker="alpaca_paper",
        )
        account_equity = authoritative_state.account_equity
        open_positions = authoritative_state.open_positions
        daily_loss_pct = authoritative_state.daily_loss_pct
        trades_today = authoritative_state.trades_today
        strategy_trades_today = authoritative_state.trades_by_strategy_today.get(signal.strategy_id, 0)
        live_sync = self.sync_alpaca_live()
        if live_sync.get("configured") and "reconciliation" in live_sync:
            sync_reconciliation = live_sync["reconciliation"]
            reconciliation = ReconciliationResult(
                ok=bool(sync_reconciliation["ok"]),
                reason=str(sync_reconciliation["reason"]),
            )
        else:
            reconciliation = reconcile_positions(
                [
                    PositionSnapshot(
                        symbol=signal.symbol,
                        internal_quantity=internal_quantity,
                        broker_quantity=broker_quantity,
                    )
                ]
            )
        loss_controls = self._effective_loss_controls(
            environment_mode=EnvironmentMode.PAPER.value,
            broker="alpaca_paper",
            daily_loss_pct=daily_loss_pct,
            weekly_loss_pct=weekly_loss_pct,
        )
        volatility_score = self._latest_volatility_score(signal.symbol)
        portfolio_state = PortfolioState(
            account_equity=account_equity,
            open_positions=open_positions,
            daily_loss_pct=loss_controls["daily_loss_pct"],
            weekly_loss_pct=loss_controls["weekly_loss_pct"],
            sector_exposure_pct=sector_exposure_pct,
            symbol_exposure_pct=symbol_exposure_pct,
            strategy_exposure_pct=strategy_exposure_pct,
            correlated_exposure_pct=correlated_exposure_pct,
            overnight_exposure_pct=overnight_exposure_pct,
            event_risk_active=event_risk_active,
            spread_bps=spread_bps,
            expected_slippage_bps=expected_slippage_bps,
            volatility_score=volatility_score,
            trades_today=trades_today,
            trades_by_strategy_today={signal.strategy_id: strategy_trades_today},
            broker_sync_ok=reconciliation.ok,
            broker_sync_reason=reconciliation.reason,
        )
        risk_decision = RiskEngine(self.settings).evaluate(signal, portfolio_state)
        risk_context = self._risk_snapshot_context(
            signal_row=signal_row,
            volatility_score=volatility_score,
            spread_bps=spread_bps,
        )
        self._capture_risk_decision_snapshot(
            signal=signal,
            signal_id=signal_row.id,
            portfolio_state=portfolio_state,
            risk_decision=risk_decision,
            risk_context=risk_context,
        )
        self._record_risk_operational_effects(
            signal=signal,
            account_equity=account_equity,
            open_positions=open_positions,
            daily_loss_pct=loss_controls["daily_loss_pct"],
            weekly_loss_pct=loss_controls["weekly_loss_pct"],
            sector_exposure_pct=sector_exposure_pct,
            symbol_exposure_pct=symbol_exposure_pct,
            strategy_exposure_pct=strategy_exposure_pct,
            correlated_exposure_pct=correlated_exposure_pct,
            overnight_exposure_pct=overnight_exposure_pct,
            trades_today=trades_today,
            strategy_trades_today=strategy_trades_today,
            reconciliation=reconciliation,
            risk_decision=risk_decision,
            volatility_score=volatility_score,
        )
        risk_row = self.repository.store_risk_check(
            risk_decision,
            signal_id=signal_row.id,
            strategy_id=signal.strategy_id,
            source_timestamp=signal.source_timestamp,
            payload={
                "account_equity": account_equity,
                "open_positions": open_positions,
                "daily_loss_pct": loss_controls["daily_loss_pct"],
                "weekly_loss_pct": loss_controls["weekly_loss_pct"],
                "input_daily_loss_pct": daily_loss_pct,
                "input_weekly_loss_pct": weekly_loss_pct,
                "broker_daily_loss_pct": loss_controls["broker_daily_loss_pct"],
                "broker_weekly_loss_pct": loss_controls["broker_weekly_loss_pct"],
                "sector_exposure_pct": sector_exposure_pct,
                "symbol_exposure_pct": symbol_exposure_pct,
                "strategy_exposure_pct": strategy_exposure_pct,
                "correlated_exposure_pct": correlated_exposure_pct,
                "overnight_exposure_pct": overnight_exposure_pct,
                "event_risk_active": event_risk_active,
                "spread_bps": spread_bps,
                "expected_slippage_bps": expected_slippage_bps,
                "volatility_score": volatility_score,
                "max_volatility_score": self.settings.max_volatility_score,
                "trades_today": trades_today,
                "strategy_trades_today": strategy_trades_today,
                "reconciliation": reconciliation.__dict__,
                "live_sync": live_sync,
            },
        )
        order = PaperExecutionEngine(settings=self.settings).submit_limit_order(
            signal=signal,
            risk_decision=risk_decision,
            reconciliation=reconciliation,
        )
        if order.quantity > 0 and order.idempotency_key:
            existing_order = self.repository.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == order.idempotency_key)
            )
            if existing_order:
                reason = "Duplicate paper order idempotency key rejected before broker call."
                self.repository.store_execution_error(
                    order_id=existing_order.id,
                    environment_mode=self.settings.environment_mode.value,
                    error_type="DUPLICATE_PAPER_ORDER",
                    reason=reason,
                    payload={
                        "idempotency_key": order.idempotency_key,
                        "signal_id": signal_row.id,
                        "existing_order_id": existing_order.id,
                    },
                )
                broker_submit = AlpacaPaperOrderResult(
                    configured=False,
                    submitted=False,
                    reason=reason,
                    broker_order_id=existing_order.broker_order_id,
                    payload={
                        "idempotency_key": order.idempotency_key,
                        "existing_order_id": existing_order.id,
                    },
                )
                return {
                    "risk_check": model_to_dict(risk_row),
                    "reconciliation": reconciliation.__dict__,
                    "order": model_to_dict(existing_order),
                    "broker_submit": broker_submit.__dict__,
                }
        order_row = self.repository.store_order(
            order,
            signal_id=signal_row.id,
            strategy_id=signal.strategy_id,
            environment_mode=self.settings.environment_mode.value,
            source_timestamp=signal.source_timestamp,
        )
        broker_submit = None
        if order.quantity > 0 and self.settings.environment_mode.value == "paper":
            adapter = AlpacaPaperAdapter(self.settings)
            broker_submit = adapter.submit_limit_bracket_order(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                stop_price=order.stop_loss,
                take_profit_price=signal.target_1,
                client_order_id=order.idempotency_key,
            )
            self.repository.store_broker_sync(
                environment_mode=self.settings.environment_mode.value,
                broker="alpaca_paper",
                success=broker_submit.submitted,
                mismatch_detected=False,
                reason=broker_submit.reason,
                payload=broker_submit.payload,
            )
            if broker_submit.submitted:
                order_row = self.repository.mark_order_broker_result(
                    order_id=order_row.id,
                    broker_order_id=broker_submit.broker_order_id,
                    status=OrderStatus.SUBMITTED.value,
                    reason=broker_submit.reason,
                )
            else:
                order_row = self.repository.mark_order_broker_result(
                    order_id=order_row.id,
                    broker_order_id=broker_submit.broker_order_id,
                    status=OrderStatus.REJECTED.value,
                    reason=broker_submit.reason,
                )
                self.repository.store_execution_error(
                    order_id=order_row.id,
                    environment_mode=self.settings.environment_mode.value,
                    error_type="BROKER_SUBMIT_FAILED",
                    reason=broker_submit.reason,
                    payload=broker_submit.payload,
                )
        return {
            "risk_check": model_to_dict(risk_row),
            "reconciliation": reconciliation.__dict__,
            "order": model_to_dict(order_row),
            "broker_submit": broker_submit.__dict__ if broker_submit else None,
        }

    def sync_alpaca_paper(self) -> AlpacaPaperSyncResult:
        adapter = AlpacaPaperAdapter(self.settings)
        result = adapter.sync()
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

    def sync_alpaca_live(self) -> dict[str, Any]:
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
        result = adapter.sync()
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

    def submit_signal_to_live(
        self,
        *,
        signal_id: str,
        weekly_loss_pct: float = 0.0,
        sector_exposure_pct: float = 0.0,
        symbol_exposure_pct: float = 0.0,
        strategy_exposure_pct: float = 0.0,
        correlated_exposure_pct: float = 0.0,
        overnight_exposure_pct: float = 0.0,
        event_risk_active: bool = False,
        spread_bps: float = 0.0,
        expected_slippage_bps: float = 0.0,
        internal_quantity: float = 0.0,
        broker_quantity: float = 0.0,
    ) -> LiveExecutionResult:
        self.bootstrap()
        signal_row = self.repository.signal_by_id(signal_id)
        if not signal_row:
            raise ValueError(f"Unknown signal id: {signal_id}")
        signal = _db_signal_to_trade_signal(signal_row)
        if self.settings.environment_mode == EnvironmentMode.LIVE:
            live_sync = self.sync_alpaca_live()
            if not live_sync.get("success"):
                raise RuntimeError(f"Unable to fetch authoritative live broker state: {live_sync.get('reason')}")
            authoritative_state = self._authoritative_portfolio_state(
                signal=signal,
                environment_mode=EnvironmentMode.LIVE.value,
                broker="alpaca_live",
            )
        else:
            authoritative_state = PortfolioState(
                account_equity=1.0,
                open_positions=0,
                daily_loss_pct=0.0,
                weekly_loss_pct=0.0,
                sector_exposure_pct=0.0,
                trades_today=0,
                trades_by_strategy_today={signal.strategy_id: 0},
            )
        account_equity = authoritative_state.account_equity
        open_positions = authoritative_state.open_positions
        daily_loss_pct = authoritative_state.daily_loss_pct
        trades_today = authoritative_state.trades_today
        strategy_trades_today = authoritative_state.trades_by_strategy_today.get(signal.strategy_id, 0)
        reconciliation = reconcile_positions(
            [
                PositionSnapshot(
                    symbol=signal.symbol,
                    internal_quantity=internal_quantity,
                    broker_quantity=broker_quantity,
                )
            ]
        )
        loss_controls = self._effective_loss_controls(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
            daily_loss_pct=daily_loss_pct,
            weekly_loss_pct=weekly_loss_pct,
        )
        volatility_score = self._latest_volatility_score(signal.symbol)
        portfolio_state = PortfolioState(
            account_equity=account_equity,
            open_positions=open_positions,
            daily_loss_pct=loss_controls["daily_loss_pct"],
            weekly_loss_pct=loss_controls["weekly_loss_pct"],
            sector_exposure_pct=sector_exposure_pct,
            symbol_exposure_pct=symbol_exposure_pct,
            strategy_exposure_pct=strategy_exposure_pct,
            correlated_exposure_pct=correlated_exposure_pct,
            overnight_exposure_pct=overnight_exposure_pct,
            event_risk_active=event_risk_active,
            spread_bps=spread_bps,
            expected_slippage_bps=expected_slippage_bps,
            volatility_score=volatility_score,
            trades_today=trades_today,
            trades_by_strategy_today={signal.strategy_id: strategy_trades_today},
            broker_sync_ok=reconciliation.ok,
            broker_sync_reason=reconciliation.reason,
            kill_switch_active=self.repository.active_kill_switch_count() > 0,
        )
        risk_decision = RiskEngine(self.settings).evaluate(signal, portfolio_state)
        risk_context = self._risk_snapshot_context(
            signal_row=signal_row,
            volatility_score=volatility_score,
            spread_bps=spread_bps,
        )
        self._capture_risk_decision_snapshot(
            signal=signal,
            signal_id=signal_row.id,
            portfolio_state=portfolio_state,
            risk_decision=risk_decision,
            risk_context=risk_context,
        )
        self._record_risk_operational_effects(
            signal=signal,
            account_equity=account_equity,
            open_positions=open_positions,
            daily_loss_pct=loss_controls["daily_loss_pct"],
            weekly_loss_pct=loss_controls["weekly_loss_pct"],
            sector_exposure_pct=sector_exposure_pct,
            symbol_exposure_pct=symbol_exposure_pct,
            strategy_exposure_pct=strategy_exposure_pct,
            correlated_exposure_pct=correlated_exposure_pct,
            overnight_exposure_pct=overnight_exposure_pct,
            trades_today=trades_today,
            strategy_trades_today=strategy_trades_today,
            reconciliation=reconciliation,
            risk_decision=risk_decision,
            volatility_score=volatility_score,
        )
        self.repository.store_risk_check(
            risk_decision,
            signal_id=signal_row.id,
            strategy_id=signal.strategy_id,
            source_timestamp=signal.source_timestamp,
            payload={
                "execution_environment": "LIVE",
                "account_equity": account_equity,
                "open_positions": open_positions,
                "daily_loss_pct": loss_controls["daily_loss_pct"],
                "weekly_loss_pct": loss_controls["weekly_loss_pct"],
                "input_daily_loss_pct": daily_loss_pct,
                "input_weekly_loss_pct": weekly_loss_pct,
                "broker_daily_loss_pct": loss_controls["broker_daily_loss_pct"],
                "broker_weekly_loss_pct": loss_controls["broker_weekly_loss_pct"],
                "sector_exposure_pct": sector_exposure_pct,
                "symbol_exposure_pct": symbol_exposure_pct,
                "strategy_exposure_pct": strategy_exposure_pct,
                "correlated_exposure_pct": correlated_exposure_pct,
                "overnight_exposure_pct": overnight_exposure_pct,
                "event_risk_active": event_risk_active,
                "spread_bps": spread_bps,
                "expected_slippage_bps": expected_slippage_bps,
                "volatility_score": volatility_score,
                "max_volatility_score": self.settings.max_volatility_score,
                "trades_today": trades_today,
                "strategy_trades_today": strategy_trades_today,
                "reconciliation": reconciliation.__dict__,
            },
        )
        return LiveExecutionService(
            self.repository,
            adapter=AlpacaLiveAdapter(self.settings),
        ).submit_limit_order(
            signal=signal,
            signal_id=signal_row.id,
            risk_decision=risk_decision,
            reconciliation=reconciliation,
        )

    def submit_internal_order_to_broker(
        self,
        *,
        order_id: str,
        actor: str = "system",
        reason: str = "Submit internal OMS order to broker.",
    ) -> dict[str, Any]:
        self.bootstrap()
        order = self.repository.session.get(models.Order, order_id)
        if not order:
            return {
                "accepted": False,
                "reason": f"Unknown order id: {order_id}.",
                "order": None,
                "broker_submit": None,
            }
        if order.status != OrderStatus.SUBMITTED.value:
            self._log_internal_order_submit_block(
                order=order,
                actor=actor,
                reason=f"Broker submission blocked for non-open status {order.status}.",
            )
            return {
                "accepted": False,
                "reason": f"Broker submission blocked for non-open status {order.status}.",
                "order": model_to_dict(order),
                "broker_submit": None,
            }
        if order.broker_order_id:
            self._log_internal_order_submit_block(
                order=order,
                actor=actor,
                reason="Broker submission blocked because order already has broker_order_id.",
            )
            return {
                "accepted": False,
                "reason": "Broker submission blocked because order already has broker_order_id.",
                "order": model_to_dict(order),
                "broker_submit": None,
            }
        if order.order_type != "market":
            self._log_internal_order_submit_block(
                order=order,
                actor=actor,
                reason="Only internal market exit orders can use this OMS broker-submit path.",
            )
            return {
                "accepted": False,
                "reason": "Only internal market exit orders can use this OMS broker-submit path.",
                "order": model_to_dict(order),
                "broker_submit": None,
            }

        signal = (
            self.repository.session.get(models.Signal, order.signal_id) if order.signal_id else None
        )
        environment = order.environment_mode
        if environment == EnvironmentMode.LIVE.value:
            gate = LiveGateService(self.repository, self.settings).evaluate(
                strategy_id=signal.strategy_id if signal else None,
                signal_id=order.signal_id,
            )
            if not gate.allowed:
                self._log_internal_order_submit_block(
                    order=order, actor=actor, reason=gate.reason, payload=gate.__dict__
                )
                return {
                    "accepted": False,
                    "reason": gate.reason,
                    "gate_decision": gate.__dict__,
                    "order": model_to_dict(order),
                    "broker_submit": None,
                }
            broker_submit = AlpacaLiveAdapter(self.settings).submit_market_order(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                client_order_id=order.idempotency_key,
            )
            broker = "alpaca_live"
            error_type = "LIVE_INTERNAL_MARKET_ORDER_SUBMIT_FAILED"
        elif (
            environment == EnvironmentMode.PAPER.value
            and self.settings.environment_mode == EnvironmentMode.PAPER
        ):
            broker_submit = AlpacaPaperAdapter(self.settings).submit_market_order(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                client_order_id=order.idempotency_key,
            )
            broker = "alpaca_paper"
            error_type = "PAPER_INTERNAL_MARKET_ORDER_SUBMIT_FAILED"
        else:
            block_reason = (
                "Internal broker submission requires matching paper mode or fully gated live mode."
            )
            self._log_internal_order_submit_block(order=order, actor=actor, reason=block_reason)
            return {
                "accepted": False,
                "reason": block_reason,
                "order": model_to_dict(order),
                "broker_submit": None,
            }

        self.repository.store_broker_sync(
            environment_mode=environment,
            broker=broker,
            success=broker_submit.submitted,
            mismatch_detected=False,
            reason=broker_submit.reason,
            payload=broker_submit.payload,
        )
        if broker_submit.submitted:
            order = self.repository.mark_order_broker_result(
                order_id=order.id,
                broker_order_id=broker_submit.broker_order_id,
                status=OrderStatus.SUBMITTED.value,
                reason=broker_submit.reason,
            )
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.CHANGED,
                entity_type="order",
                entity_id=order.id,
                strategy_id=signal.strategy_id if signal else None,
                rule_version="internal_order_broker_submit_v1",
                reason=reason,
                payload={"broker_submit": broker_submit.__dict__, "actor": actor},
                source_timestamp=datetime.now(UTC),
            )
        else:
            order = self.repository.mark_order_broker_result(
                order_id=order.id,
                broker_order_id=broker_submit.broker_order_id,
                status=OrderStatus.REJECTED.value,
                reason=broker_submit.reason,
            )
            self.repository.store_execution_error(
                order_id=order.id,
                environment_mode=environment,
                error_type=error_type,
                reason=broker_submit.reason,
                payload=broker_submit.payload,
            )
        return {
            "accepted": broker_submit.submitted,
            "reason": broker_submit.reason,
            "order": model_to_dict(order),
            "broker_submit": broker_submit.__dict__,
        }

    def _log_internal_order_submit_block(
        self,
        *,
        order: models.Order,
        actor: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.BLOCKED,
            entity_type="order",
            entity_id=order.id,
            strategy_id=None,
            rule_version="internal_order_broker_submit_v1",
            reason=reason,
            payload={"order": model_to_dict(order), "actor": actor, **(payload or {})},
            source_timestamp=datetime.now(UTC),
        )

    def _capture_risk_decision_snapshot(
        self,
        *,
        signal: TradeSignal,
        signal_id: str,
        portfolio_state: PortfolioState,
        risk_decision: RiskDecision,
        risk_context: dict[str, Any],
    ) -> None:
        DecisionSnapshotService(self.repository).capture_risk_decision(
            signal=signal,
            signal_id=signal_id,
            portfolio_state=portfolio_state,
            risk_decision=risk_decision,
            risk_context=risk_context,
            source_timestamp=signal.source_timestamp,
        )

    def _authoritative_portfolio_state(
        self,
        *,
        signal: TradeSignal,
        environment_mode: str,
        broker: str,
    ) -> PortfolioState:
        loss_controls = self._effective_loss_controls(
            environment_mode=environment_mode,
            broker=broker,
            daily_loss_pct=0.0,
            weekly_loss_pct=0.0,
        )
        return PortfolioService(self.repository).build_state(
            signal=signal,
            environment_mode=environment_mode,
            broker=broker,
            loss_controls=loss_controls,
        )

    def _risk_snapshot_context(
        self,
        *,
        signal_row: models.Signal,
        volatility_score: float | None,
        spread_bps: float,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "volatility_score": volatility_score,
            "spread_bps": spread_bps,
        }
        version = self.repository.session.scalar(
            select(models.SignalVersion)
            .where(models.SignalVersion.signal_id == signal_row.id)
            .order_by(desc(models.SignalVersion.created_at))
            .limit(1)
        )
        if version and isinstance(version.payload, dict):
            if version.payload.get("regime_reference"):
                context["regime_state"] = {"reference": version.payload["regime_reference"]}
            if version.payload.get("catalyst_reference"):
                context["catalyst_state"] = {"reference": version.payload["catalyst_reference"]}
        return context

    def _record_risk_operational_effects(
        self,
        *,
        signal: TradeSignal,
        account_equity: float,
        open_positions: int,
        daily_loss_pct: float,
        weekly_loss_pct: float,
        sector_exposure_pct: float,
        symbol_exposure_pct: float,
        strategy_exposure_pct: float,
        correlated_exposure_pct: float,
        overnight_exposure_pct: float,
        trades_today: int,
        strategy_trades_today: int,
        reconciliation: ReconciliationResult,
        risk_decision: Any,
        volatility_score: float | None,
    ) -> None:
        entry_price = float(signal.entry_zone[0])
        proposed_notional = float(risk_decision.position_size or 0) * entry_price
        proposed_symbol_exposure_pct = (
            proposed_notional / account_equity * 100 if account_equity > 0 else 0.0
        )
        self.repository.store_exposure_snapshot(
            account_equity=account_equity,
            total_exposure=proposed_symbol_exposure_pct,
            sector_exposure={"UNKNOWN": sector_exposure_pct},
            strategy_exposure={
                signal.strategy_id: max(strategy_exposure_pct, proposed_symbol_exposure_pct)
            },
            symbol_exposure={
                signal.symbol: max(symbol_exposure_pct, proposed_symbol_exposure_pct),
                "correlated": correlated_exposure_pct,
                "overnight": overnight_exposure_pct,
            },
            reason=(
                "Risk evaluation exposure snapshot. "
                f"open_positions={open_positions}; trades_today={trades_today}; "
                f"strategy_trades_today={strategy_trades_today}."
            ),
        )
        if volatility_score is not None and volatility_score >= self.settings.max_volatility_score:
            self.repository.activate_kill_switch(
                event_type="VOLATILITY_BREACH",
                reason=(
                    f"Volatility score {volatility_score:.2f} reached configured limit "
                    f"{self.settings.max_volatility_score:.2f}."
                ),
                payload={
                    "symbol": signal.symbol,
                    "strategy_id": signal.strategy_id,
                    "volatility_score": volatility_score,
                    "max_volatility_score": self.settings.max_volatility_score,
                    "risk_decision": risk_decision.reason,
                },
            )
        if not reconciliation.ok:
            self.repository.activate_kill_switch(
                event_type="FAILED_RECONCILIATION",
                reason=reconciliation.reason,
                payload={
                    "symbol": signal.symbol,
                    "strategy_id": signal.strategy_id,
                    "risk_decision": risk_decision.reason,
                },
            )
        if daily_loss_pct >= self.settings.max_daily_loss_pct:
            self.repository.activate_kill_switch(
                event_type="DAILY_LOSS_LIMIT",
                reason=(
                    f"Daily loss {daily_loss_pct:.2f}% reached configured limit "
                    f"{self.settings.max_daily_loss_pct:.2f}%."
                ),
                payload={"symbol": signal.symbol, "strategy_id": signal.strategy_id},
            )
        if weekly_loss_pct >= self.settings.max_weekly_loss_pct:
            self.repository.activate_kill_switch(
                event_type="WEEKLY_LOSS_LIMIT",
                reason=(
                    f"Weekly loss {weekly_loss_pct:.2f}% reached configured limit "
                    f"{self.settings.max_weekly_loss_pct:.2f}%."
                ),
                payload={"symbol": signal.symbol, "strategy_id": signal.strategy_id},
            )

    def _latest_volatility_score(self, symbol: str) -> float | None:
        feature = self.repository.latest_daily_feature_for(symbol)
        return (
            float(feature.volatility_score)
            if feature and feature.volatility_score is not None
            else None
        )

    def _effective_loss_controls(
        self,
        *,
        environment_mode: str,
        broker: str,
        daily_loss_pct: float,
        weekly_loss_pct: float,
    ) -> dict[str, float | None]:
        broker_daily = self.repository.broker_equity_loss_pct(
            environment_mode=environment_mode,
            broker=broker,
            lookback=timedelta(days=1),
        )
        broker_weekly = self.repository.broker_equity_loss_pct(
            environment_mode=environment_mode,
            broker=broker,
            lookback=timedelta(days=7),
        )
        effective_daily = max(daily_loss_pct, broker_daily or 0.0)
        effective_weekly = max(weekly_loss_pct, broker_weekly or 0.0)
        return {
            "daily_loss_pct": effective_daily,
            "weekly_loss_pct": effective_weekly,
            "broker_daily_loss_pct": broker_daily,
            "broker_weekly_loss_pct": broker_weekly,
        }

    def cancel_all_live_orders(self, *, actor: str, reason: str) -> dict[str, Any]:
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
            result = AlpacaLiveAdapter(self.settings).cancel_all_orders().__dict__
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

    def flatten_all_live_positions(self, *, actor: str, reason: str) -> dict[str, Any]:
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
            result = AlpacaLiveAdapter(self.settings).flatten_all_positions().__dict__
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

    async def run_alpaca_market_data_stream(
        self,
        *,
        symbols: list[str] | None = None,
        channels: list[str] | None = None,
        max_messages: int | None = 25,
    ) -> AlpacaStreamRunResult:
        self.bootstrap()
        stream = AlpacaMarketDataStream(self.repository, self.settings)
        return await stream.run(symbols=symbols, channels=channels, max_messages=max_messages)

    def run_fill_reconciliation_once(self) -> FillReconciliationResult:
        self.bootstrap()
        return FillReconciliationLoop(self.repository, self.settings).run_once()

    def collect_news(self, symbols: list[str] | None = None) -> NewsCollectionResult:
        self.bootstrap()
        return AlphaVantageNewsCollector(self.repository, self.settings).collect(symbols)

    def collect_sec_filings(
        self,
        symbols: list[str] | None = None,
        *,
        max_filings_per_symbol: int = 10,
    ) -> SecCollectionResult:
        self.bootstrap()
        return SecEdgarCollector(self.repository, self.settings).collect(
            symbols,
            max_filings_per_symbol=max_filings_per_symbol,
        )

    def run_scheduled_job(
        self,
        job_name: str,
        *,
        symbols: list[str] | None = None,
        actor: str = "system",
    ) -> ScheduledJobResult:
        self.bootstrap()
        return ScheduledCollectorRunner(self.repository, self.settings).run_once(
            job_name,
            symbols=symbols,
            actor=actor,
        )

    def run_provider_health(self) -> ProviderHealthRunResult:
        self.bootstrap()
        return ProviderHealthService(self.repository, self.settings).run_once()

    def run_features(self, symbols: list[str] | None = None) -> FeatureRunResult:
        self.bootstrap()
        return ProductionFeatureEngine(self.repository).run_once(symbols)

    def run_market_regime(self) -> RegimeRunResult:
        self.bootstrap()
        return MarketRegimeService(self.repository).run_once()

    def run_catalysts(self, symbols: list[str] | None = None) -> CatalystRunResult:
        self.bootstrap()
        return CatalystEngine(self.repository).run_once(symbols)

    def run_production_scanners(
        self, symbols: list[str] | None = None
    ) -> ProductionScannerRunResult:
        self.bootstrap()
        return ProductionScannerEngine(self.repository).run_once(symbols)

    def run_trade_monitor(self) -> TradeMonitorRunResult:
        self.bootstrap()
        return TradeMonitorService(self.repository).run_once()

    def run_reviews(self) -> ReviewRunResult:
        self.bootstrap()
        return TradeReviewEngine(self.repository).run_once()

    def run_learning_review(self) -> LearningRunResult:
        self.bootstrap()
        return LearningRecommendationEngine(self.repository).run_weekly_review()

    def generate_live_readiness_report(self, *, actor: str = "system") -> LiveReadinessResult:
        self.bootstrap()
        return LiveReadinessService(self.repository, self.settings).generate_report(actor=actor)

    def refresh_universe(self, symbols: list[str] | None = None) -> UniverseRefreshResult:
        self.bootstrap()
        return LiquidUniverseBuilder(self.repository, self.settings).refresh(symbols)

    def repair_missing_candles(self, symbols: list[str] | None = None) -> MissingCandleRepairResult:
        self.bootstrap()
        return MissingCandleRepairService(self.repository, self.settings).run_once(symbols)

    def request_strategy_status_change(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        requested_status: str,
        requested_by: str,
        evidence: dict[str, Any],
        reason: str,
    ) -> StrategyStatusRequestResult:
        self.bootstrap()
        return StrategyApprovalWorkflow(self.repository).request_status_change(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            requested_status=requested_status,
            requested_by=requested_by,
            evidence=evidence,
            reason=reason,
        )

    def approve_strategy_status_change(
        self,
        *,
        request_id: str,
        approved: bool,
        decided_by: str,
        decision_reason: str,
    ) -> StrategyStatusDecisionResult:
        self.bootstrap()
        return StrategyApprovalWorkflow(self.repository).approve_status_change(
            request_id=request_id,
            approved=approved,
            decided_by=decided_by,
            decision_reason=decision_reason,
        )

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

    def run_backtest(
        self,
        *,
        strategy_id: str = "VWAP_RECLAIM",
        symbol: str = "SPY",
        provider: str = "alpaca_market_data",
    ) -> dict[str, Any]:
        self.bootstrap()
        frame = self.repository.clean_candles_df(
            symbol,
            provider=provider,
            limit=2_000,
            valid_only=True,
        )
        if frame.empty:
            frame = self.repository.clean_candles_df(
                symbol,
                provider="yahoo_chart",
                limit=2_000,
                valid_only=True,
            )
            provider = "yahoo_chart"
        assumptions = BacktestAssumptions()
        if len(frame) < 30:
            metrics = {
                "error": "At least 30 clean candles are required for a VWAP reclaim backtest."
            }
            report = self.repository.store_backtest_report(
                strategy_id=strategy_id,
                strategy_version="v1",
                universe_name=f"{symbol}:{provider}",
                assumptions=assumptions.__dict__,
                metrics=metrics,
                report_uri=None,
                survivorship_bias_warning=SURVIVORSHIP_BIAS_WARNING,
                reason="Backtest rejected due to insufficient clean candles.",
            )
            return {"report": model_to_dict(report), "metrics": metrics}
        relative_volume = frame["volume"] / frame["volume"].rolling(20, min_periods=1).mean()
        try:
            result = run_vwap_reclaim_backtest(
                frame,
                relative_volume=relative_volume,
                assumptions=assumptions,
            )
            metrics = result["stats"]
            reason = "VectorBT VWAP reclaim backtest completed."
        except Exception as exc:
            metrics = {"error": str(exc)}
            reason = "Backtest failed; error was persisted for review."
        report = self.repository.store_backtest_report(
            strategy_id=strategy_id,
            strategy_version="v1",
            universe_name=f"{symbol}:{provider}",
            assumptions=assumptions.__dict__,
            metrics=metrics,
            report_uri=None,
            survivorship_bias_warning=SURVIVORSHIP_BIAS_WARNING,
            reason=reason,
        )
        return {"report": model_to_dict(report), "metrics": metrics}

    def dashboard_snapshot(self) -> dict[str, Any]:
        self.bootstrap()
        return {
            "counts": self.repository.counts(),
            "active_symbols": self.repository.active_symbols(),
            "clean_candles": self.repository.latest_clean_candles(200),
            "features": self.repository.latest_features(100),
            "daily_features": self.repository.latest_daily_features(100),
            "regime_snapshots": self.repository.latest_regime_snapshots(100),
            "scanner_results": self.repository.latest_scanner_results(100),
            "signals": self.repository.latest_signals(100),
            "trade_theses": self.repository.latest_trade_theses(100),
            "risk_checks": self.repository.latest_risk_checks(100),
            "broker_account_snapshots": self.repository.latest_broker_account_snapshots(100),
            "orders": self.repository.latest_orders(100),
            "positions": self.repository.latest_positions(100),
            "exposure_snapshots": self.repository.latest_exposure_snapshots(100),
            "execution_errors": self.repository.latest_execution_errors(100),
            "journal": self.repository.latest_journal(100),
            "decisions": self.repository.latest_decisions(200),
            "audit_logs": self.repository.latest_audit_logs(200),
            "api_calls": self.repository.latest_api_calls(100),
            "data_quality_errors": self.repository.latest_data_quality_errors(100),
            "provider_health": self.repository.latest_provider_health(100),
            "provider_rate_limits": self.repository.latest_provider_rate_limits(100),
            "worker_heartbeats": self.repository.latest_worker_heartbeats(100),
            "stream_events": self.repository.latest_stream_events(100),
            "scheduler_runs": self.repository.latest_scheduler_runs(100),
            "clean_news": self.repository.latest_clean_news(100),
            "filings": self.repository.latest_filings(100),
            "events": self.repository.latest_events(100),
            "catalysts": self.repository.latest_catalysts(100),
            "fills": self.repository.latest_fills(100),
            "broker_sync_logs": self.repository.latest_broker_sync_logs(100),
            "ai_reviews": self.repository.latest_ai_reviews(100),
            "weekly_reviews": self.repository.latest_weekly_reviews(20),
            "strategy_recommendations": self.repository.latest_strategy_recommendations(100),
            "backtest_reports": self.repository.latest_backtest_reports(50),
            "live_readiness_reports": self.repository.latest_live_readiness_reports(20),
            "live_trading_approvals": self.repository.latest_live_trading_approvals(20),
            "kill_switches": self.repository.latest_kill_switches(100),
            "strategy_approval_requests": self.repository.latest_strategy_approval_requests(100),
            "missing_candle_gaps": self.repository.latest_missing_candle_gaps(100),
            "opportunity_scores": self.repository.latest_opportunity_scores(100),
            "alpha_rejections": self.repository.latest_alpha_rejections(100),
            "expectancy_snapshots": self.repository.latest_expectancy_snapshots(100),
            "sector_strength": self.repository.latest_sector_strength(100),
            "symbol_relative_strength": self.repository.latest_symbol_relative_strength(100),
            "point_in_time_universe": self.repository.latest_point_in_time_universe_memberships(
                100
            ),
            "short_interest": self.repository.latest_short_interest_snapshots(100),
            "options_intelligence": self.repository.latest_options_intelligence_snapshots(100),
            "multi_bagger_candidates": self.repository.latest_multi_bagger_candidate_scores(100),
            "providers": self.repository.list_rows(models.ProviderCapability, 100),
            "strategies": self.repository.list_rows(models.StrategyRegistry, 100),
        }

    def _build_vwap_snapshot(
        self,
        symbol: str,
        frame: pd.DataFrame,
    ) -> tuple[VwapReclaimSnapshot, dict[str, Any]]:
        ordered = frame.sort_index()
        vwap = calculate_vwap(ordered)
        atr = calculate_atr(ordered)
        latest = ordered.iloc[-1]
        previous = ordered.iloc[-2]
        session_volume = float(ordered["volume"].sum())
        current_volume = float(latest["volume"])
        relative_volume = calculate_relative_volume(
            current_volume, max(1.0, float(ordered["volume"].tail(20).mean()))
        )
        dollar_volume = float((ordered["close"] * ordered["volume"]).sum())
        spread_bps = calculate_spread_bps(float(latest["low"]), float(latest["high"]))
        volume_spike_score = calculate_volume_spike_score(relative_volume)
        liquidity_score = min(100.0, dollar_volume / max(1.0, self.settings.min_dollar_volume) * 50)
        spread_score = max(0.0, 100.0 - spread_bps)
        snapshot = VwapReclaimSnapshot(
            symbol=symbol,
            timestamp=ordered.index[-1].to_pydatetime(),
            price=float(latest["close"]),
            previous_price=float(previous["close"]),
            vwap=float(vwap.iloc[-1]),
            previous_vwap=float(vwap.iloc[-2]),
            relative_volume=relative_volume,
            average_volume=session_volume,
            dollar_volume=dollar_volume,
            spread_bps=spread_bps,
            market_regime=MarketRegime.CHOPPY,
            has_catalyst=False,
            strong_relative_strength=bool(float(latest["close"]) > float(vwap.iloc[-1])),
        )
        feature_payload = {
            "symbol": symbol,
            "source_timestamp": snapshot.timestamp.isoformat(),
            "feature_version": FEATURE_CALCULATION_VERSION,
            "price": snapshot.price,
            "vwap": snapshot.vwap,
            "atr": float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
            "relative_volume": relative_volume,
            "volume_spike_score": volume_spike_score,
            "liquidity_score": liquidity_score,
            "spread_score": spread_score,
            "spread_bps": spread_bps,
            "dollar_volume": dollar_volume,
            "session_volume_so_far": session_volume,
            "spread_note": "Proxy from current candle high/low because Yahoo chart has no bid/ask quote.",
        }
        return snapshot, feature_payload


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
