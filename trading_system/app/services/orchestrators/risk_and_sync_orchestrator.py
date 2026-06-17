from __future__ import annotations

from trading_system.app.services.runtime_support import *  # noqa: F403,F401


class RiskAndSyncOrchestrator:
    """Broker synchronization, reconciliation, and live account control orchestration."""

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
