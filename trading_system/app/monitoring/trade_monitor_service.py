from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select

from trading_system.app.core.enums import DecisionOutcome, DecisionType, Direction, OrderStatus, TradeType
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.execution.order_manager import OrderManager
from trading_system.app.execution.order_side import (
    entry_side_from_direction,
    exit_side_from_direction,
    normalize_order_side,
)
from trading_system.app.monitoring.trade_monitor import evaluate_day_trade_to_swing_conversion


TRADE_MONITOR_VERSION = "trade_monitor_v1"


@dataclass(frozen=True)
class TradeMonitorRunResult:
    positions_seen: int
    decisions_recorded: int
    journal_entries_created: int
    journal_entries_updated: int
    rule_violations_recorded: int
    stale_orders_cancelled: int
    protective_exit_orders_created: int
    stop_orders_adjusted: int
    reason: str
    version: str = TRADE_MONITOR_VERSION


class TradeMonitorService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self) -> TradeMonitorRunResult:
        stale_result = OrderManager(self.repository).cancel_stale_orders()
        positions = self.repository.session.scalars(
            select(models.Position).order_by(desc(models.Position.created_at)).limit(500)
        ).all()
        decisions = journal_entries = 0
        for position in positions:
            if position.quantity == 0:
                continue
            latest_candle = self.repository.session.scalar(
                select(models.CleanMarketData)
                .where(models.CleanMarketData.symbol == position.symbol)
                .order_by(desc(models.CleanMarketData.source_timestamp))
                .limit(1)
            )
            unrealized = None
            if latest_candle and position.average_price:
                unrealized = (latest_candle.close - position.average_price) * position.quantity
            decision = evaluate_day_trade_to_swing_conversion(
                profitable=bool(unrealized is not None and unrealized > 0),
                close_near_high_of_day=bool(latest_candle and latest_candle.close >= latest_candle.high * 0.98),
                volume_confirms=bool(latest_candle and latest_candle.volume > 0),
                catalyst_still_valid=True,
                overnight_risk_approved=False,
                market_regime_supportive=False,
            )
            self.repository.store_decision_log(
                decision_type=DecisionType.EXECUTION,
                outcome=DecisionOutcome.RECORDED,
                entity_type="position",
                entity_id=position.id,
                strategy_id=None,
                rule_version=TRADE_MONITOR_VERSION,
                reason=f"{decision.action}: {decision.reason}",
                payload={
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                    "latest_price": latest_candle.close if latest_candle else None,
                    "unrealized_pnl": unrealized,
                },
                source_timestamp=datetime.now(UTC),
            )
            decisions += 1
        lifecycle = self._sync_journal_lifecycle()
        journal_entries += lifecycle["created"]
        return TradeMonitorRunResult(
            positions_seen=len(positions),
            decisions_recorded=decisions,
            journal_entries_created=journal_entries,
            journal_entries_updated=lifecycle["updated"],
            rule_violations_recorded=lifecycle["rule_violations"],
            stale_orders_cancelled=stale_result.orders_changed,
            protective_exit_orders_created=lifecycle["protective_exits"],
            stop_orders_adjusted=lifecycle["stop_orders_adjusted"],
            reason="Trade monitor cycle recorded position decisions and journal lifecycle metrics.",
        )

    def _sync_journal_lifecycle(self) -> dict[str, int]:
        created = updated = rule_violations = protective_exits = stop_orders_adjusted = 0
        fills = self.repository.session.execute(
            select(models.Fill, models.Order)
            .join(models.Order, models.Fill.order_id == models.Order.id)
            .where(models.Order.signal_id.isnot(None))
            .order_by(models.Fill.source_timestamp.asc(), models.Fill.created_at.asc())
            .limit(1000)
        ).all()
        fills_by_signal: dict[str, list[tuple[models.Fill, models.Order]]] = {}
        for fill, order in fills:
            if not order.signal_id:
                continue
            fills_by_signal.setdefault(order.signal_id, []).append((fill, order))

        for signal_id, signal_fills in fills_by_signal.items():
            signal = self.repository.session.get(models.Signal, signal_id)
            lifecycle = self.repository.persist_journal_lifecycle_for_signal(signal_id=signal_id)
            metrics = lifecycle["metrics"]
            if not metrics:
                continue
            if lifecycle["created"]:
                created += 1
            if lifecycle["updated"]:
                updated += 1
            if self._needs_protective_exit(metrics):
                result = OrderManager(self.repository).request_protective_exit_order(
                    signal_id=signal_id,
                    strategy_id=signal.strategy_id if signal else None,
                    symbol=metrics["symbol"],
                    side=metrics["exit_side"],
                    quantity=metrics["open_quantity"],
                    environment_mode=signal_fills[0][1].environment_mode,
                    execution_environment=signal_fills[0][1].execution_environment,
                    broker=signal_fills[0][1].broker,
                    reason="Stop loss breached; protective exit order recorded by trade monitor.",
                    reference_price=metrics["latest_price"],
                )
                protective_exits += result.orders_changed
            stop_orders_adjusted += self._move_stop_orders_to_breakeven(
                signal=signal,
                signal_fills=signal_fills,
                metrics=metrics,
            )
            rule_violations += len(metrics["rule_violations"])
        return {
            "created": created,
            "updated": updated,
            "rule_violations": rule_violations,
            "protective_exits": protective_exits,
            "stop_orders_adjusted": stop_orders_adjusted,
        }

    def _build_journal_metrics(
        self,
        *,
        signal: models.Signal | None,
        signal_fills: list[tuple[models.Fill, models.Order]],
    ) -> dict[str, Any] | None:
        if not signal_fills:
            return None
        symbol = signal.symbol if signal else signal_fills[0][1].symbol
        entry_side = self._entry_side(signal=signal, fallback_side=signal_fills[0][1].side)
        exit_side = (
            exit_side_from_direction(signal.direction)
            if signal
            else ("buy" if entry_side == "sell" else "sell")
        )
        entry_fills = [(fill, order) for fill, order in signal_fills if order.side.lower() == entry_side]
        exit_fills = [(fill, order) for fill, order in signal_fills if order.side.lower() == exit_side]
        if not entry_fills:
            return None

        entry_quantity = sum(fill.quantity for fill, _order in entry_fills)
        exit_quantity = sum(fill.quantity for fill, _order in exit_fills)
        if entry_quantity <= 0:
            return None

        actual_entry = _weighted_average([(fill.price, fill.quantity) for fill, _order in entry_fills])
        actual_exit = _weighted_average([(fill.price, fill.quantity) for fill, _order in exit_fills])
        slippage_bps = _weighted_average(
            [
                (fill.slippage_bps, fill.quantity)
                for fill, _order in entry_fills + exit_fills
                if fill.slippage_bps is not None
            ]
        )
        first_entry_at = min(_as_utc(fill.source_timestamp) for fill, _order in entry_fills)
        latest_candle = self._latest_candle(symbol)
        latest_candle_at = _as_utc(latest_candle.source_timestamp) if latest_candle else None
        fully_exited = exit_quantity >= entry_quantity and exit_quantity > 0
        last_exit_at = (
            max(_as_utc(fill.source_timestamp) for fill, _order in exit_fills) if exit_fills else None
        )
        end_at = (
            last_exit_at
            if fully_exited and last_exit_at is not None
            else latest_candle_at or last_exit_at or datetime.now(UTC)
        )
        high, low = self._price_excursion(symbol=symbol, start_at=first_entry_at, end_at=end_at)
        if high is None and actual_exit is not None:
            high = max(actual_entry, actual_exit)
        if low is None and actual_exit is not None:
            low = min(actual_entry, actual_exit)
        high = high if high is not None else actual_entry
        low = low if low is not None else actual_entry

        direction = signal.direction if signal else Direction.LONG.value
        signed_multiplier = -1.0 if direction == Direction.SHORT.value else 1.0
        exited_quantity = min(exit_quantity, entry_quantity)
        pnl = None
        if actual_exit is not None and exited_quantity > 0:
            commissions = sum((fill.commission or 0.0) for fill, _order in entry_fills + exit_fills)
            pnl = (actual_exit - actual_entry) * exited_quantity * signed_multiplier - commissions

        if direction == Direction.SHORT.value:
            max_favorable = max(0.0, (actual_entry - low) * entry_quantity)
            max_adverse = min(0.0, (actual_entry - high) * entry_quantity)
        else:
            max_favorable = max(0.0, (high - actual_entry) * entry_quantity)
            max_adverse = min(0.0, (low - actual_entry) * entry_quantity)

        open_quantity = max(0.0, entry_quantity - exit_quantity)
        rule_violations = self._journal_rule_violations(
            signal=signal,
            latest_candle=latest_candle,
            first_entry_at=first_entry_at,
            open_quantity=open_quantity,
            entry_side=entry_side,
        )
        if fully_exited:
            change_reason = "Journal lifecycle updated after full exit fill reconciliation."
        elif exit_quantity > 0:
            change_reason = "Journal lifecycle updated after partial exit fill reconciliation."
        else:
            change_reason = "Journal lifecycle updated after entry fill reconciliation."

        return {
            "symbol": symbol,
            "actual_entry": actual_entry,
            "actual_exit": actual_exit,
            "latest_price": latest_candle.close if latest_candle else actual_exit,
            "exit_side": exit_side,
            "open_quantity": open_quantity,
            "pnl": pnl,
            "max_favorable_excursion": max_favorable,
            "max_adverse_excursion": max_adverse,
            "slippage_bps": slippage_bps,
            "time_in_trade_seconds": max(0.0, (end_at - first_entry_at).total_seconds()),
            "rule_violations": rule_violations,
            "change_reason": change_reason,
        }

    @staticmethod
    def _needs_protective_exit(metrics: dict[str, Any]) -> bool:
        return metrics["open_quantity"] > 0 and "STOP_LOSS_BREACHED" in metrics["rule_violations"]

    def _move_stop_orders_to_breakeven(
        self,
        *,
        signal: models.Signal | None,
        signal_fills: list[tuple[models.Fill, models.Order]],
        metrics: dict[str, Any],
    ) -> int:
        if not signal or metrics["open_quantity"] <= 0:
            return 0
        actual_entry = metrics["actual_entry"]
        latest_price = metrics["latest_price"]
        if actual_entry is None or latest_price is None or signal.stop_loss is None:
            return 0
        direction = signal.direction or Direction.LONG.value
        if direction == Direction.SHORT.value:
            initial_risk = signal.stop_loss - actual_entry
            one_r_reached = latest_price <= actual_entry - initial_risk
        else:
            initial_risk = actual_entry - signal.stop_loss
            one_r_reached = latest_price >= actual_entry + initial_risk
        if initial_risk <= 0 or not one_r_reached:
            return 0

        open_statuses = {OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value}
        exit_side = metrics["exit_side"]
        orders = self.repository.session.scalars(
            select(models.Order)
            .where(models.Order.signal_id == signal.id)
            .where(models.Order.status.in_(open_statuses))
            .where(models.Order.side == exit_side)
            .order_by(models.Order.created_at.asc())
        ).all()
        adjusted = 0
        for order in orders:
            if order.stop_loss is None:
                continue
            if direction == Direction.SHORT.value:
                already_protected = order.stop_loss <= actual_entry
            else:
                already_protected = order.stop_loss >= actual_entry
            if already_protected:
                continue
            result = OrderManager(self.repository).request_replace_order(
                order_id=order.id,
                new_stop_loss=actual_entry,
                reason="Trade monitor moved stop to breakeven after 1R favorable move.",
                actor="system",
            )
            if result.success:
                adjusted += 1
        return adjusted

    def _price_excursion(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[float | None, float | None]:
        return self.repository.session.execute(
            select(func.max(models.CleanMarketData.high), func.min(models.CleanMarketData.low)).where(
                models.CleanMarketData.symbol == symbol.upper(),
                models.CleanMarketData.source_timestamp >= start_at,
                models.CleanMarketData.source_timestamp <= end_at,
            )
        ).one()

    def _latest_candle(self, symbol: str) -> models.CleanMarketData | None:
        return self.repository.session.scalar(
            select(models.CleanMarketData)
            .where(models.CleanMarketData.symbol == symbol.upper())
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(1)
        )

    def _journal_rule_violations(
        self,
        *,
        signal: models.Signal | None,
        latest_candle: models.CleanMarketData | None,
        first_entry_at: datetime,
        open_quantity: float,
        entry_side: str,
    ) -> list[str]:
        if not signal or open_quantity <= 0:
            return []
        violations = []
        latest_at = _as_utc(latest_candle.source_timestamp) if latest_candle else datetime.now(UTC)
        if signal.trade_type == TradeType.DAY_TRADE.value and latest_at.date() > first_entry_at.date():
            violations.append("DAY_TRADE_TO_SWING_BLOCKED")
        if latest_candle and signal.stop_loss:
            stop_breached = (
                latest_candle.close <= signal.stop_loss
                if entry_side == "buy"
                else latest_candle.close >= signal.stop_loss
            )
            if stop_breached:
                violations.append("STOP_LOSS_BREACHED")
        return violations

    @staticmethod
    def _entry_side(*, signal: models.Signal | None, fallback_side: str) -> str:
        if signal:
            return entry_side_from_direction(signal.direction)
        return normalize_order_side(fallback_side)

    @staticmethod
    def _journal_changed(journal: models.TradeJournal, metrics: dict[str, Any]) -> bool:
        checks = {
            "actual_entry": metrics["actual_entry"],
            "actual_exit": metrics["actual_exit"],
            "pnl": metrics["pnl"],
            "max_favorable_excursion": metrics["max_favorable_excursion"],
            "max_adverse_excursion": metrics["max_adverse_excursion"],
            "slippage_bps": metrics["slippage_bps"],
            "time_in_trade_seconds": metrics["time_in_trade_seconds"],
            "rule_violations": metrics["rule_violations"],
        }
        for field, expected in checks.items():
            current = getattr(journal, field)
            if isinstance(expected, float) or isinstance(current, float):
                if expected is None or current is None:
                    if expected != current:
                        return True
                elif abs(float(current) - float(expected)) > 0.0001:
                    return True
            elif current != expected:
                return True
        return False


def _weighted_average(values: list[tuple[float | None, float]]) -> float | None:
    weighted_values = [(value, weight) for value, weight in values if value is not None and weight > 0]
    total_weight = sum(weight for _value, weight in weighted_values)
    if total_weight <= 0:
        return None
    return sum(float(value) * weight for value, weight in weighted_values if value is not None) / total_weight


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
