from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, EnvironmentMode, OrderStatus
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.execution.alpaca_live_adapter import AlpacaLiveAdapter
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter
from trading_system.app.risk.live_gates import LiveGateService


ORDER_MANAGER_VERSION = "order_manager_v1"


@dataclass(frozen=True)
class OrderManagerResult:
    success: bool
    orders_seen: int
    orders_changed: int
    reason: str
    payload: dict[str, Any]
    version: str = ORDER_MANAGER_VERSION


class OrderManager:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def cancel_stale_orders(self) -> OrderManagerResult:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.max_order_stale_seconds)
        rows = self.repository.session.scalars(
            select(models.Order).where(
                models.Order.status == OrderStatus.SUBMITTED.value,
                models.Order.created_at < cutoff,
            )
        ).all()
        changed = 0
        payload_rows = []
        for row in rows:
            cancel_result = self._cancel_broker_order_if_required(row)
            if cancel_result is not None and not cancel_result["success"]:
                self.repository.store_execution_error(
                    order_id=row.id,
                    environment_mode=row.environment_mode,
                    error_type="STALE_ORDER_CANCEL_FAILED",
                    reason=cancel_result["reason"],
                    payload=cancel_result,
                )
                payload_rows.append({"order": model_to_dict(row), "broker_cancel": cancel_result})
                continue
            row.status = OrderStatus.STALE_CANCELLED.value
            row.cancelled_at = datetime.now(UTC)
            row.rejection_reason = "Order exceeded configured stale-order threshold."
            changed += 1
            payload_rows.append({"order": model_to_dict(row), "broker_cancel": cancel_result})
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.BLOCKED,
                entity_type="order",
                entity_id=row.id,
                strategy_id=None,
                rule_version=ORDER_MANAGER_VERSION,
                reason="Stale order cancelled in internal order manager.",
                payload={
                    "max_order_stale_seconds": self.settings.max_order_stale_seconds,
                    "broker_cancel": cancel_result,
                },
            )
        self.repository.session.commit()
        return OrderManagerResult(
            success=True,
            orders_seen=len(rows),
            orders_changed=changed,
            reason="Stale order cancellation cycle completed.",
            payload={"orders": payload_rows},
        )

    def request_replace_order(
        self,
        *,
        order_id: str,
        reason: str,
        actor: str = "system",
        new_limit_price: float | None = None,
        new_stop_loss: float | None = None,
    ) -> OrderManagerResult:
        row = self.repository.session.get(models.Order, order_id)
        if not row:
            result = OrderManagerResult(False, 0, 0, f"Unknown order id: {order_id}.", {})
            self.repository.store_audit_log(
                actor=actor,
                event_type="ORDER_REPLACE_BLOCKED",
                entity_type="order",
                entity_id=order_id,
                reason=result.reason,
                payload={"requested_reason": reason},
            )
            return result
        if row.status not in {OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value}:
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.BLOCKED,
                entity_type="order",
                entity_id=row.id,
                strategy_id=None,
                rule_version=ORDER_MANAGER_VERSION,
                reason=f"Replace order blocked for non-open status {row.status}.",
                payload=model_to_dict(row),
            )
            result = OrderManagerResult(
                False,
                1,
                0,
                f"Replace order blocked for non-open status {row.status}.",
                {"order": model_to_dict(row)},
            )
            self.repository.store_audit_log(
                actor=actor,
                event_type="ORDER_REPLACE_BLOCKED",
                entity_type="order",
                entity_id=row.id,
                reason=result.reason,
                payload={"requested_reason": reason, "order": model_to_dict(row)},
            )
            return result

        broker_cancel = self._cancel_broker_order_if_required(row)
        if broker_cancel is not None and not broker_cancel["success"]:
            self.repository.store_execution_error(
                order_id=row.id,
                environment_mode=row.environment_mode,
                error_type="REPLACE_ORDER_CANCEL_FAILED",
                reason=broker_cancel["reason"],
                payload={"broker_cancel": broker_cancel, "requested_reason": reason},
            )
            result = OrderManagerResult(
                False,
                1,
                0,
                "Replace order blocked because existing broker order could not be cancelled.",
                {"order": model_to_dict(row), "broker_cancel": broker_cancel},
            )
            self.repository.store_audit_log(
                actor=actor,
                event_type="ORDER_REPLACE_BLOCKED",
                entity_type="order",
                entity_id=row.id,
                reason=result.reason,
                payload={
                    "requested_reason": reason,
                    "order": model_to_dict(row),
                    "broker_cancel": broker_cancel,
                },
            )
            return result

        previous = model_to_dict(row)
        row.status = OrderStatus.CANCELLED.value
        row.cancelled_at = datetime.now(UTC)
        row.rejection_reason = f"Order replaced: {reason}"
        replacement = models.Order(
            signal_id=row.signal_id,
            idempotency_key=f"{row.idempotency_key}:replace:{int(datetime.now(UTC).timestamp())}",
            environment_mode=row.environment_mode,
            execution_environment=row.execution_environment,
            broker=row.broker,
            broker_order_id=None,
            symbol=row.symbol,
            side=row.side,
            quantity=row.quantity,
            order_type=row.order_type,
            limit_price=new_limit_price if new_limit_price is not None else row.limit_price,
            stop_loss=new_stop_loss if new_stop_loss is not None else row.stop_loss,
            status=OrderStatus.SUBMITTED.value,
            rejection_reason=None,
            expected_price=new_limit_price if new_limit_price is not None else row.expected_price,
            submitted_at=datetime.now(UTC),
            source_timestamp=datetime.now(UTC),
        )
        self.repository.session.add(replacement)
        self.repository.session.commit()
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.CHANGED,
            entity_type="order",
            entity_id=replacement.id,
            strategy_id=None,
            rule_version=ORDER_MANAGER_VERSION,
            reason=f"Order replaced internally: {reason}",
            payload={
                "previous_order": previous,
                "replacement_order": model_to_dict(replacement),
                "new_limit_price": new_limit_price,
                "new_stop_loss": new_stop_loss,
                "broker_cancel": broker_cancel,
                "broker_submission_required": True,
            },
        )
        result = OrderManagerResult(
            success=True,
            orders_seen=1,
            orders_changed=2,
            reason="Replacement order recorded internally; broker submission remains gated.",
            payload={
                "previous_order": model_to_dict(row),
                "replacement_order": model_to_dict(replacement),
                "broker_cancel": broker_cancel,
            },
        )
        self.repository.store_audit_log(
            actor=actor,
            event_type="ORDER_REPLACED",
            entity_type="order",
            entity_id=replacement.id,
            reason=reason,
            payload={
                "previous_order_id": row.id,
                "replacement_order_id": replacement.id,
                "new_limit_price": new_limit_price,
                "new_stop_loss": new_stop_loss,
                "broker_cancel": broker_cancel,
            },
        )
        return result

    def request_protective_exit_order(
        self,
        *,
        signal_id: str | None,
        strategy_id: str | None,
        symbol: str,
        side: str,
        quantity: float,
        environment_mode: str,
        execution_environment: str,
        broker: str,
        reason: str,
        reference_price: float | None = None,
    ) -> OrderManagerResult:
        if quantity <= 0:
            return OrderManagerResult(False, 0, 0, "Protective exit quantity must be positive.", {})
        normalized_symbol = symbol.upper()
        normalized_side = side.lower()
        idempotency_key = (
            f"protective-exit:{signal_id or normalized_symbol}:"
            f"{normalized_side}:{environment_mode}"
        )
        existing = self.repository.session.scalar(
            select(models.Order).where(models.Order.idempotency_key == idempotency_key)
        )
        if existing:
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.BLOCKED,
                entity_type="order",
                entity_id=existing.id,
                strategy_id=strategy_id,
                rule_version=ORDER_MANAGER_VERSION,
                reason="Duplicate protective exit order blocked before broker submission.",
                payload=model_to_dict(existing),
            )
            return OrderManagerResult(
                False,
                1,
                0,
                "Duplicate protective exit order blocked before broker submission.",
                {"order": model_to_dict(existing)},
            )

        row = models.Order(
            signal_id=signal_id,
            idempotency_key=idempotency_key,
            environment_mode=environment_mode,
            execution_environment=execution_environment,
            broker=broker,
            broker_order_id=None,
            symbol=normalized_symbol,
            side=normalized_side,
            quantity=quantity,
            order_type="market",
            limit_price=None,
            stop_loss=None,
            status=OrderStatus.SUBMITTED.value,
            rejection_reason=None,
            expected_price=reference_price,
            submitted_at=datetime.now(UTC),
            source_timestamp=datetime.now(UTC),
        )
        self.repository.session.add(row)
        self.repository.session.commit()
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.CHANGED,
            entity_type="order",
            entity_id=row.id,
            strategy_id=strategy_id,
            rule_version=ORDER_MANAGER_VERSION,
            reason=reason,
            payload={
                "protective_exit": True,
                "quantity": quantity,
                "reference_price": reference_price,
                "broker_submission_required": True,
            },
        )
        return OrderManagerResult(
            success=True,
            orders_seen=1,
            orders_changed=1,
            reason="Protective exit order recorded internally; broker submission remains gated.",
            payload={"order": model_to_dict(row)},
        )

    def _cancel_broker_order_if_required(self, row: models.Order) -> dict[str, Any] | None:
        if not row.broker_order_id:
            return None
        if row.environment_mode == EnvironmentMode.LIVE.value:
            gate = LiveGateService(self.repository, self.settings).evaluate(
                strategy_id=None,
                signal_id=row.signal_id,
            )
            if not gate.allowed:
                return {
                    "success": False,
                    "reason": gate.reason,
                    "gate_decision": gate.__dict__,
                    "broker_order_id": row.broker_order_id,
                }
            result = AlpacaLiveAdapter(self.settings).cancel_order(row.broker_order_id)
            return result.__dict__
        if row.environment_mode == EnvironmentMode.PAPER.value:
            result = AlpacaPaperAdapter(self.settings).cancel_order(row.broker_order_id)
            return result.__dict__
        return {
            "success": False,
            "reason": f"Unsupported environment for stale broker cancellation: {row.environment_mode}.",
            "broker_order_id": row.broker_order_id,
        }
