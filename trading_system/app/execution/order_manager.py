from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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


@dataclass(frozen=True)
class BrokerOrderEventResult:
    success: bool
    duplicate: bool
    orders_seen: int
    orders_changed: int
    reason: str
    order: dict[str, Any] | None
    fill: dict[str, Any] | None
    payload: dict[str, Any]
    version: str = ORDER_MANAGER_VERSION


class OrderManager:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def build_client_order_id(
        *,
        namespace: str,
        symbol: str,
        strategy_id: str | None,
        source_timestamp: datetime,
        side: str,
        leg: str = "entry",
    ) -> str:
        raw = "|".join(
            [
                namespace.strip().lower(),
                symbol.strip().upper(),
                (strategy_id or "none").strip().upper(),
                source_timestamp.isoformat(),
                side.strip().lower(),
                leg.strip().lower(),
            ]
        )
        return f"oms-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}-{leg.strip().lower()}"

    def request_bracket_order(
        self,
        *,
        signal_id: str | None,
        strategy_id: str | None,
        symbol: str,
        side: str,
        quantity: float,
        limit_price: float,
        stop_loss: float,
        take_profit_price: float,
        environment_mode: str,
        execution_environment: str,
        broker: str,
        source_timestamp: datetime | None = None,
        reason: str = "Bracket order accepted by OMS.",
    ) -> OrderManagerResult:
        source_timestamp = source_timestamp or datetime.now(UTC)
        normalized_symbol = symbol.strip().upper()
        normalized_side = side.strip().lower()
        if normalized_side not in {"buy", "sell"}:
            return OrderManagerResult(False, 0, 0, "Bracket order side must be buy or sell.", {})
        if quantity <= 0:
            return OrderManagerResult(False, 0, 0, "Bracket order quantity must be positive.", {})
        if limit_price <= 0 or stop_loss <= 0 or take_profit_price <= 0:
            return OrderManagerResult(False, 0, 0, "Bracket order prices must be positive.", {})

        exit_side = "sell" if normalized_side == "buy" else "buy"
        leg_specs = [
            ("entry", normalized_side, "limit", limit_price, stop_loss, OrderStatus.SUBMITTED.value),
            ("take_profit", exit_side, "limit", take_profit_price, None, OrderStatus.CREATED.value),
            ("stop_loss", exit_side, "stop", None, stop_loss, OrderStatus.CREATED.value),
        ]
        client_order_ids = {
            leg: self.build_client_order_id(
                namespace="bracket",
                symbol=normalized_symbol,
                strategy_id=strategy_id,
                source_timestamp=source_timestamp,
                side=normalized_side,
                leg=leg,
            )
            for leg, *_ in leg_specs
        }
        existing = self.repository.session.scalars(
            select(models.Order).where(models.Order.idempotency_key.in_(client_order_ids.values()))
        ).all()
        if existing:
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.BLOCKED,
                entity_type="order",
                entity_id=existing[0].id,
                strategy_id=strategy_id,
                rule_version=ORDER_MANAGER_VERSION,
                reason="Duplicate bracket ClOrdID rejected before broker submission.",
                payload={
                    "client_order_ids": client_order_ids,
                    "existing_orders": [model_to_dict(row) for row in existing],
                },
                source_timestamp=source_timestamp,
            )
            return OrderManagerResult(
                False,
                len(existing),
                0,
                "Duplicate bracket ClOrdID rejected before broker submission.",
                {"orders": [model_to_dict(row) for row in existing], "client_order_ids": client_order_ids},
            )

        rows = []
        for leg, leg_side, order_type, leg_limit, leg_stop, status in leg_specs:
            row = models.Order(
                signal_id=signal_id,
                idempotency_key=client_order_ids[leg],
                environment_mode=environment_mode,
                execution_environment=execution_environment,
                broker=broker,
                broker_order_id=None,
                symbol=normalized_symbol,
                side=leg_side,
                quantity=quantity,
                order_type=order_type,
                limit_price=leg_limit,
                stop_loss=leg_stop,
                status=status,
                rejection_reason=None,
                expected_price=leg_limit,
                submitted_at=datetime.now(UTC) if status == OrderStatus.SUBMITTED.value else None,
                source_timestamp=source_timestamp,
            )
            self.repository.session.add(row)
            rows.append((leg, row))
        try:
            self.repository.session.commit()
        except IntegrityError:
            self.repository.session.rollback()
            self.logger.warning(
                "Idempotency collision for bracket order keys: %s",
                sorted(client_order_ids.values()),
            )
            existing = self.repository.session.scalars(
                select(models.Order).where(models.Order.idempotency_key.in_(client_order_ids.values()))
            ).all()
            return OrderManagerResult(
                False,
                len(existing),
                0,
                "Duplicate idempotency key rejected during bracket order commit.",
                {"orders": [model_to_dict(row) for row in existing], "client_order_ids": client_order_ids},
            )
        payload = {
            "order_class": "bracket",
            "client_order_ids": client_order_ids,
            "legs": {leg: model_to_dict(row) for leg, row in rows},
        }
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.APPROVED,
            entity_type="order",
            entity_id=rows[0][1].id,
            strategy_id=strategy_id,
            rule_version=ORDER_MANAGER_VERSION,
            reason=reason,
            payload=payload,
            source_timestamp=source_timestamp,
        )
        return OrderManagerResult(
            True,
            3,
            3,
            "Bracket order legs recorded with strict ClOrdIDs.",
            payload,
        )

    def apply_broker_order_event(
        self,
        *,
        broker_order: dict[str, Any],
        environment_mode: str,
        broker: str = "alpaca_live",
    ) -> BrokerOrderEventResult:
        client_order_id = str(broker_order.get("client_order_id") or "").strip()
        broker_order_id = str(broker_order.get("id") or "").strip()
        if not client_order_id and not broker_order_id:
            reason = "Broker order event ignored because it has no ClOrdID or broker order id."
            self.repository.store_execution_error(
                order_id=None,
                environment_mode=environment_mode,
                error_type="BROKER_ORDER_EVENT_MISSING_ID",
                reason=reason,
                payload=broker_order,
            )
            return BrokerOrderEventResult(False, False, 0, 0, reason, None, None, {"broker_order": broker_order})

        row = None
        if client_order_id:
            row = self.repository.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == client_order_id)
            )
        if not row and broker_order_id:
            row = self.repository.session.scalar(
                select(models.Order).where(models.Order.broker_order_id == broker_order_id)
            )
        if not row:
            reason = "Unknown broker order event ignored; OMS will not create live orders from webhooks."
            self.repository.store_audit_log(
                actor="system",
                event_type="UNKNOWN_BROKER_ORDER_EVENT_IGNORED",
                entity_type="order",
                entity_id=client_order_id or broker_order_id,
                reason=reason,
                payload={"broker": broker, "environment_mode": environment_mode, "broker_order": broker_order},
            )
            return BrokerOrderEventResult(True, True, 0, 0, reason, None, None, {"broker_order": broker_order})

        previous = model_to_dict(row)
        next_status = self._normalize_broker_status(str(broker_order.get("status") or row.status))
        fill = self.repository.store_broker_fill_from_order(order=row, broker_order=broker_order)
        if (
            row.status == next_status
            and (not broker_order_id or row.broker_order_id == broker_order_id)
            and fill is None
        ):
            reason = "Duplicate broker order event ignored; no OMS state changed."
            self.repository.store_audit_log(
                actor="system",
                event_type="DUPLICATE_BROKER_ORDER_EVENT_IGNORED",
                entity_type="order",
                entity_id=row.id,
                reason=reason,
                payload={"broker_order": broker_order, "previous_order": previous},
            )
            return BrokerOrderEventResult(
                True,
                True,
                1,
                0,
                reason,
                model_to_dict(row),
                None,
                {"broker_order": broker_order},
            )

        row.broker_order_id = broker_order_id or row.broker_order_id
        row.status = next_status
        if next_status == OrderStatus.REJECTED.value:
            row.rejection_reason = (
                str(
                    broker_order.get("failed_reason")
                    or broker_order.get("reject_reason")
                    or broker_order.get("reason")
                    or "Broker reported order rejection."
                )
            )
        if next_status in {OrderStatus.CANCELLED.value, OrderStatus.STALE_CANCELLED.value}:
            row.cancelled_at = datetime.now(UTC)
        row.limit_price = self._float_or_none(broker_order.get("limit_price")) or row.limit_price
        self.repository.session.commit()

        if next_status == OrderStatus.REJECTED.value and previous["status"] != OrderStatus.REJECTED.value:
            self.repository.store_execution_error(
                order_id=row.id,
                environment_mode=environment_mode,
                error_type="BROKER_ORDER_REJECTED",
                reason=row.rejection_reason or "Broker reported order rejection.",
                payload={"broker_order": broker_order, "previous_order": previous},
            )
        self.repository.store_decision_log(
            decision_type=DecisionType.EXECUTION,
            outcome=DecisionOutcome.CHANGED,
            entity_type="order",
            entity_id=row.id,
            strategy_id=None,
            rule_version=ORDER_MANAGER_VERSION,
            reason="Broker order event applied to OMS state.",
            payload={
                "previous_order": previous,
                "order": model_to_dict(row),
                "fill": model_to_dict(fill) if fill else None,
                "broker_order": broker_order,
            },
        )
        return BrokerOrderEventResult(
            True,
            False,
            1,
            1,
            "Broker order event applied to OMS state.",
            model_to_dict(row),
            model_to_dict(fill) if fill else None,
            {"broker_order": broker_order},
        )

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
        try:
            self.repository.session.commit()
        except IntegrityError:
            self.repository.session.rollback()
            self.logger.warning("Idempotency collision for replacement order key: %s", replacement.idempotency_key)
            existing = self.repository.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == replacement.idempotency_key)
            )
            return OrderManagerResult(
                False,
                1 if existing else 0,
                0,
                "Duplicate idempotency key rejected during replacement order commit.",
                {"order": model_to_dict(existing) if existing else None},
            )
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
        try:
            self.repository.session.commit()
        except IntegrityError:
            self.repository.session.rollback()
            self.logger.warning("Idempotency collision for key: %s", idempotency_key)
            existing = self.repository.session.scalar(
                select(models.Order).where(models.Order.idempotency_key == idempotency_key)
            )
            return OrderManagerResult(
                False,
                1 if existing else 0,
                0,
                "Duplicate idempotency key rejected during protective exit order commit.",
                {"order": model_to_dict(existing) if existing else None},
            )
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

    @staticmethod
    def _normalize_broker_status(status: str) -> str:
        normalized = status.strip().lower()
        if normalized in {"new", "accepted", "pending_new", "accepted_for_bidding", "submitted"}:
            return OrderStatus.SUBMITTED.value
        if normalized in {"partially_filled", "partial_fill", "partial"}:
            return OrderStatus.PARTIALLY_FILLED.value
        if normalized in {"filled", "done_for_day"}:
            return OrderStatus.FILLED.value
        if normalized in {"canceled", "cancelled"}:
            return OrderStatus.CANCELLED.value
        if normalized in {"expired", "stopped"}:
            return OrderStatus.STALE_CANCELLED.value
        if normalized in {"rejected", "suspended", "calculated"}:
            return OrderStatus.REJECTED.value
        return status.strip().upper()

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
