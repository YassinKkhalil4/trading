from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import EnvironmentMode, OrderStatus
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.execution.alpaca_live_adapter import AlpacaLiveAdapter
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter
from trading_system.app.execution.reconciliation import PositionSnapshot, reconcile_positions


@dataclass(frozen=True)
class FillReconciliationResult:
    configured: bool
    success: bool
    orders_seen: int
    fills_recorded: int
    positions_seen: int
    mismatch_detected: bool
    reason: str


class FillReconciliationLoop:
    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings | None = None,
        adapter=None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.adapter = adapter

    def run_once(self) -> FillReconciliationResult:
        live_mode = self.settings.environment_mode == EnvironmentMode.LIVE
        adapter = self.adapter or (AlpacaLiveAdapter(self.settings) if live_mode else AlpacaPaperAdapter(self.settings))
        broker = "alpaca_live" if live_mode else "alpaca_paper"
        sync = adapter.sync()
        if not sync.configured:
            self.repository.store_broker_sync(
                environment_mode=self.settings.environment_mode.value,
                broker=broker,
                success=False,
                mismatch_detected=False,
                reason=sync.reason,
                payload=None,
            )
            return FillReconciliationResult(False, False, 0, 0, 0, False, sync.reason)
        if not sync.success:
            self.repository.store_broker_sync(
                environment_mode=self.settings.environment_mode.value,
                broker=broker,
                success=False,
                mismatch_detected=False,
                reason=sync.reason,
                payload=None,
            )
            self.repository.activate_kill_switch(
                event_type="BROKER_SYNC_FAILURE",
                reason=sync.reason,
                payload={"broker": broker, "environment_mode": self.settings.environment_mode.value},
            )
            return FillReconciliationResult(True, False, 0, 0, 0, False, sync.reason)

        self.repository.store_broker_account_snapshot(
            environment_mode=self.settings.environment_mode.value,
            broker=broker,
            account=sync.account,
            reason=f"{broker} account snapshot captured during fill reconciliation.",
        )
        fills_recorded = 0
        for broker_order in sync.orders:
            order = self.repository.update_order_from_broker(
                broker_order=broker_order,
                environment_mode=self.settings.environment_mode.value,
            )
            if order:
                self._handle_broker_order_status(order=order, broker_order=broker_order, broker=broker)
                fill = self.repository.store_broker_fill_from_order(
                    order=order,
                    broker_order=broker_order,
                )
                if fill:
                    fills_recorded += 1
                    self._handle_fill_risk(fill=fill, order=order, broker=broker)

        snapshots = []
        for position in sync.positions:
            symbol = str(position.get("symbol") or "").upper()
            if not symbol:
                continue
            broker_qty = float(position.get("qty") or 0.0)
            internal = self.repository.position_for(
                environment_mode=self.settings.environment_mode.value,
                symbol=symbol,
            )
            internal_qty = float(internal.quantity) if internal else 0.0
            mismatch = round(internal_qty, 6) != round(broker_qty, 6)
            snapshots.append(
                PositionSnapshot(symbol=symbol, internal_quantity=internal_qty, broker_quantity=broker_qty)
            )
            self.repository.upsert_position(
                environment_mode=self.settings.environment_mode.value,
                symbol=symbol,
                quantity=internal_qty,
                average_price=internal.average_price if internal else None,
                broker_quantity=broker_qty,
                broker_average_price=_float_or_none(position.get("avg_entry_price")),
                reconciliation_status="MISMATCH_PENDING_REVIEW" if mismatch else "SYNCED",
                reason=(
                    f"Broker/internal mismatch detected from {broker} account."
                    if mismatch
                    else f"Position reconciled from {broker} account."
                ),
            )

        reconciliation = reconcile_positions(snapshots)
        self.repository.store_broker_sync(
            environment_mode=self.settings.environment_mode.value,
            broker=broker,
            success=reconciliation.ok,
            mismatch_detected=not reconciliation.ok,
            reason=reconciliation.reason,
            payload={
                "orders_seen": len(sync.orders),
                "fills_recorded": fills_recorded,
                "positions_seen": len(sync.positions),
                "account": sync.account,
            },
        )
        if not reconciliation.ok:
            self.repository.activate_kill_switch(
                event_type="FAILED_RECONCILIATION",
                reason=reconciliation.reason,
                payload={
                    "broker": broker,
                    "environment_mode": self.settings.environment_mode.value,
                    "orders_seen": len(sync.orders),
                    "positions_seen": len(sync.positions),
                },
            )
        return FillReconciliationResult(
            configured=True,
            success=reconciliation.ok,
            orders_seen=len(sync.orders),
            fills_recorded=fills_recorded,
            positions_seen=len(sync.positions),
            mismatch_detected=not reconciliation.ok,
            reason=reconciliation.reason,
        )

    def _handle_broker_order_status(self, *, order, broker_order: dict, broker: str) -> None:
        if order.status != OrderStatus.REJECTED.value:
            return
        if self.repository.execution_error_exists(order_id=order.id, error_type="BROKER_ORDER_REJECTED"):
            return
        reason = order.rejection_reason or "Broker reported order rejection."
        self.repository.store_execution_error(
            order_id=order.id,
            environment_mode=self.settings.environment_mode.value,
            error_type="BROKER_ORDER_REJECTED",
            reason=reason,
            payload={
                "broker": broker,
                "broker_order_id": order.broker_order_id,
                "client_order_id": broker_order.get("client_order_id"),
                "broker_order": broker_order,
            },
        )

    def _handle_fill_risk(self, *, fill, order, broker: str) -> None:
        if fill.slippage_bps is None or fill.slippage_bps <= self.settings.max_slippage_bps:
            return
        reason = (
            f"Slippage breach on {fill.symbol}: {fill.slippage_bps:.2f} bps exceeds "
            f"limit {self.settings.max_slippage_bps:.2f} bps."
        )
        self.repository.activate_kill_switch(
            event_type="SLIPPAGE_BREACH",
            reason=reason,
            payload={
                "fill_id": fill.id,
                "order_id": order.id,
                "broker": broker,
                "symbol": fill.symbol,
                "slippage_bps": fill.slippage_bps,
                "max_slippage_bps": self.settings.max_slippage_bps,
            },
        )


def _float_or_none(value):
    if value in (None, ""):
        return None
    return float(value)
