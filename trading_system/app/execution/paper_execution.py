from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, EnvironmentMode, OrderStatus
from trading_system.app.execution.order_side import entry_side_from_direction
from trading_system.app.execution.reconciliation import ReconciliationResult
from trading_system.app.risk.risk_engine import RiskDecision
from trading_system.app.signals.idempotency import IdempotencyRegistry, build_idempotency_key
from trading_system.app.signals.signal_engine import TradeSignal


EXECUTION_RULE_VERSION = "paper_execution_v1"


@dataclass(frozen=True)
class PaperOrder:
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float
    stop_loss: float
    idempotency_key: str
    status: OrderStatus
    reason: str
    created_at: datetime


class PaperExecutionEngine:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        idempotency_registry: IdempotencyRegistry | None = None,
        decision_logger: InMemoryDecisionLogger | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.idempotency_registry = idempotency_registry or IdempotencyRegistry()
        self.decision_logger = decision_logger or InMemoryDecisionLogger()

    def submit_limit_order(
        self,
        *,
        signal: TradeSignal,
        risk_decision: RiskDecision,
        reconciliation: ReconciliationResult,
    ) -> PaperOrder:
        if self.settings.environment_mode != EnvironmentMode.PAPER:
            return self._blocked_order(signal, "Paper execution requires ENVIRONMENT_MODE=paper.")
        if not risk_decision.approved:
            return self._blocked_order(signal, f"Risk rejected order: {risk_decision.reason}")
        if not reconciliation.ok:
            return self._blocked_order(signal, reconciliation.reason)

        order_key = build_idempotency_key(
            namespace="order",
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
            source_timestamp=signal.source_timestamp,
            direction=signal.direction.value,
        )
        self.idempotency_registry.reserve(order_key)
        order = PaperOrder(
            symbol=signal.symbol,
            side=entry_side_from_direction(signal.direction),
            quantity=risk_decision.position_size,
            order_type="limit",
            limit_price=signal.entry_zone[0],
            stop_loss=signal.stop_loss,
            idempotency_key=order_key,
            status=OrderStatus.SUBMITTED,
            reason="Paper limit order accepted for simulated submission.",
            created_at=datetime.now(UTC),
        )
        self.decision_logger.record_simple(
            DecisionType.EXECUTION,
            DecisionOutcome.APPROVED,
            order.reason,
            entity_id=order_key,
            strategy_id=signal.strategy_id,
            rule_version=EXECUTION_RULE_VERSION,
        )
        return order

    def _blocked_order(self, signal: TradeSignal, reason: str) -> PaperOrder:
        order = PaperOrder(
            symbol=signal.symbol,
            side=entry_side_from_direction(signal.direction),
            quantity=0,
            order_type="limit",
            limit_price=signal.entry_zone[0],
            stop_loss=signal.stop_loss,
            idempotency_key="",
            status=OrderStatus.REJECTED,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self.decision_logger.record_simple(
            DecisionType.EXECUTION,
            DecisionOutcome.BLOCKED,
            reason,
            entity_id=signal.idempotency_key,
            strategy_id=signal.strategy_id,
            rule_version=EXECUTION_RULE_VERSION,
        )
        return order


class LiveExecutionEngine:
    def submit_order(self, *_args, **_kwargs):
        raise RuntimeError(
            "Use LiveExecutionService with explicit live gates; live trading is disabled by default."
        )
