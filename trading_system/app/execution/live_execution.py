from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from trading_system.app.core.enums import (
    DecisionOutcome,
    DecisionType,
    EnvironmentMode,
    ExecutionEnvironment,
    OrderStatus,
)
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.execution.alpaca_live_adapter import (
    AlpacaLiveAdapter,
    AlpacaLiveEmergencyResult,
    AlpacaLiveOrderResult,
)
from trading_system.app.execution.broker_adapter import AbstractBrokerAdapter
from trading_system.app.execution.order_manager import (
    MAX_CHILD_NOTIONAL,
    OrderManager,
    TWAP_Order_Manager,
)
from trading_system.app.execution.order_side import entry_side_from_direction
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.execution.reconciliation import ReconciliationResult
from trading_system.app.risk.live_gates import LIVE_GATE_VERSION, LiveGateService
from trading_system.app.risk.risk_engine import RiskDecision
from trading_system.app.signals.signal_engine import TradeSignal


LIVE_EXECUTION_RULE_VERSION = "live_execution_v1"


@dataclass(frozen=True)
class LiveExecutionResult:
    accepted: bool
    reason: str
    gate_decision: dict[str, Any]
    order: dict[str, Any] | None
    broker_submit: dict[str, Any] | None
    rule_version: str = LIVE_EXECUTION_RULE_VERSION


class LiveExecutionService:
    def __init__(
        self,
        repository: TradingRepository,
        *,
        adapter: AbstractBrokerAdapter | None = None,
    ) -> None:
        self.repository = repository
        self.adapter = adapter or AlpacaLiveAdapter()

    async def submit_limit_order(
        self,
        *,
        signal: TradeSignal,
        signal_id: str,
        risk_decision: RiskDecision,
        reconciliation: ReconciliationResult,
    ) -> LiveExecutionResult:
        gate = LiveGateService(self.repository, self.adapter.settings).evaluate(
            strategy_id=signal.strategy_id,
            signal_id=signal_id,
        )
        if not gate.allowed:
            return self._blocked(signal, signal_id, gate.reason, gate.__dict__)
        if not risk_decision.approved:
            return self._blocked(
                signal,
                signal_id,
                f"Risk rejected live order: {risk_decision.reason}",
                gate.__dict__,
            )
        if not reconciliation.ok:
            return self._blocked(signal, signal_id, reconciliation.reason, gate.__dict__)

        reference_price = signal.entry_zone[0]
        twap_manager = TWAP_Order_Manager(self.repository, self.adapter.settings)
        if twap_manager.should_slice(
            quantity=risk_decision.position_size, reference_price=reference_price
        ):
            twap = twap_manager.schedule_parent_order(
                signal_id=signal_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                side=entry_side_from_direction(signal.direction),
                quantity=risk_decision.position_size,
                reference_price=reference_price,
                source_timestamp=signal.source_timestamp,
            )
            return LiveExecutionResult(
                accepted=twap.success,
                reason=twap.reason,
                gate_decision=gate.__dict__,
                order={
                    "parent_signal_id": signal_id,
                    "child_order_count": len(twap.child_orders),
                    "max_child_notional": MAX_CHILD_NOTIONAL,
                },
                broker_submit={
                    "submitted": False,
                    "twap_scheduled": twap.success,
                    "payload": twap.payload,
                },
            )

        order_key = OrderManager.build_client_order_id(
            namespace="live_order",
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
            source_timestamp=signal.source_timestamp,
            side=entry_side_from_direction(signal.direction),
            leg="entry",
        )
        existing_order = self.repository.session.scalar(
            select(models.Order).where(models.Order.idempotency_key == order_key)
        )
        if existing_order:
            reason = "Duplicate live order idempotency key rejected before broker call."
            self.repository.store_execution_error(
                order_id=existing_order.id,
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="DUPLICATE_LIVE_ORDER",
                reason=reason,
                payload={
                    "idempotency_key": order_key,
                    "signal_id": signal_id,
                    "existing_order_id": existing_order.id,
                },
            )
            return LiveExecutionResult(
                accepted=False,
                reason=reason,
                gate_decision=gate.__dict__,
                order=model_to_dict(existing_order),
                broker_submit=AlpacaLiveOrderResult(
                    configured=self.adapter.configured,
                    submitted=False,
                    reason=reason,
                    broker_order_id=None,
                    payload=None,
                ).__dict__,
            )
        local_order = PaperOrder(
            symbol=signal.symbol,
            side=entry_side_from_direction(signal.direction),
            quantity=risk_decision.position_size,
            order_type="limit",
            limit_price=signal.entry_zone[0],
            stop_loss=signal.stop_loss,
            idempotency_key=order_key,
            status=OrderStatus.SUBMITTED,
            reason="Live limit bracket order passed local gates and is ready for broker submission.",
            created_at=datetime.now(UTC),
        )
        order_row = self.repository.store_order(
            local_order,
            signal_id=signal_id,
            strategy_id=signal.strategy_id,
            environment_mode=EnvironmentMode.LIVE.value,
            execution_environment=ExecutionEnvironment.LIVE.value,
            broker="alpaca_live",
            source_timestamp=signal.source_timestamp,
        )
        broker_submit = await self.adapter.submit_limit_bracket_order(
            symbol=local_order.symbol,
            side=local_order.side,
            quantity=local_order.quantity,
            limit_price=local_order.limit_price,
            stop_price=local_order.stop_loss,
            take_profit_price=signal.target_1,
            client_order_id=local_order.idempotency_key,
        )
        self.repository.store_broker_sync(
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
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
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="LIVE_BROKER_SUBMIT_FAILED",
                reason=broker_submit.reason,
                payload=broker_submit.payload,
            )
        return LiveExecutionResult(
            accepted=broker_submit.submitted,
            reason=broker_submit.reason,
            gate_decision=gate.__dict__,
            order=model_to_dict(order_row),
            broker_submit=broker_submit.__dict__,
        )

    async def cancel_all_live_orders(self) -> AlpacaLiveEmergencyResult:
        gate = LiveGateService(self.repository, self.adapter.settings).evaluate_operational_action(
            action="live_cancel_all_orders"
        )
        if not gate.allowed:
            result = AlpacaLiveEmergencyResult(
                configured=self.adapter.configured,
                success=False,
                reason=gate.reason,
                payload={"gate_decision": gate.__dict__},
            )
            self.repository.store_execution_error(
                order_id=None,
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="LIVE_CANCEL_ALL_BLOCKED",
                reason=gate.reason,
                payload=gate.__dict__,
            )
        else:
            result = await self.adapter.cancel_all_orders()
            if not result.success:
                self.repository.store_execution_error(
                    order_id=None,
                    environment_mode=EnvironmentMode.LIVE.value,
                    error_type="LIVE_CANCEL_ALL_FAILED",
                    reason=result.reason,
                    payload=result.payload,
                )
        self.repository.store_audit_log(
            actor="system",
            event_type="LIVE_CANCEL_ALL_ORDERS",
            entity_type="execution",
            entity_id=None,
            reason=result.reason,
            payload={
                "success": result.success,
                "payload": result.payload,
                "gate_decision": gate.__dict__,
            },
        )
        return result

    async def flatten_all_live_positions(self) -> AlpacaLiveEmergencyResult:
        gate = LiveGateService(self.repository, self.adapter.settings).evaluate_operational_action(
            action="live_flatten_all_positions"
        )
        if not gate.allowed:
            result = AlpacaLiveEmergencyResult(
                configured=self.adapter.configured,
                success=False,
                reason=gate.reason,
                payload={"gate_decision": gate.__dict__},
            )
            self.repository.store_execution_error(
                order_id=None,
                environment_mode=EnvironmentMode.LIVE.value,
                error_type="LIVE_FLATTEN_ALL_BLOCKED",
                reason=gate.reason,
                payload=gate.__dict__,
            )
        else:
            result = await self.adapter.flatten_all_positions()
            if not result.success:
                self.repository.store_execution_error(
                    order_id=None,
                    environment_mode=EnvironmentMode.LIVE.value,
                    error_type="LIVE_FLATTEN_ALL_FAILED",
                    reason=result.reason,
                    payload=result.payload,
                )
        self.repository.store_audit_log(
            actor="system",
            event_type="LIVE_FLATTEN_ALL_POSITIONS",
            entity_type="execution",
            entity_id=None,
            reason=result.reason,
            payload={
                "success": result.success,
                "payload": result.payload,
                "gate_decision": gate.__dict__,
            },
        )
        return result

    def _blocked(
        self,
        signal: TradeSignal,
        signal_id: str,
        reason: str,
        gate_payload: dict[str, Any],
    ) -> LiveExecutionResult:
        blocked_order = PaperOrder(
            symbol=signal.symbol,
            side=entry_side_from_direction(signal.direction),
            quantity=0,
            order_type="limit",
            limit_price=signal.entry_zone[0],
            stop_loss=signal.stop_loss,
            idempotency_key=f"blocked-live-{signal.idempotency_key}",
            status=OrderStatus.REJECTED,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        row = self.repository.store_order(
            blocked_order,
            signal_id=signal_id,
            strategy_id=signal.strategy_id,
            environment_mode=EnvironmentMode.LIVE.value,
            execution_environment=ExecutionEnvironment.LIVE_DISABLED.value,
            broker="alpaca_live",
            source_timestamp=signal.source_timestamp,
        )
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.BLOCKED,
            entity_type="live_order_gate",
            entity_id=row.id,
            strategy_id=signal.strategy_id,
            rule_version=LIVE_GATE_VERSION,
            reason=reason,
            payload=gate_payload,
            source_timestamp=signal.source_timestamp,
        )
        return LiveExecutionResult(
            accepted=False,
            reason=reason,
            gate_decision=gate_payload,
            order=model_to_dict(row),
            broker_submit=AlpacaLiveOrderResult(False, False, reason, None, None).__dict__,
        )
