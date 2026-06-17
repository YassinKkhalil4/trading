from __future__ import annotations

from trading_system.app.services.runtime_support import *  # noqa: F403,F401


class ExecutionOrchestrator:
    """Order submission orchestration for paper, live, and internal broker orders."""

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

    def _build_paper_order_candidate(
        self,
        *,
        signal: Any,
        risk_decision: Any,
        reconciliation: ReconciliationResult,
    ) -> PaperOrder:
        if self.settings.environment_mode != EnvironmentMode.PAPER:
            reason = "Paper execution requires ENVIRONMENT_MODE=paper."
            quantity = 0
            status = OrderStatus.REJECTED
            idempotency_key = ""
        elif not risk_decision.approved:
            reason = f"Risk rejected order: {risk_decision.reason}"
            quantity = 0
            status = OrderStatus.REJECTED
            idempotency_key = ""
        elif not reconciliation.ok:
            reason = reconciliation.reason
            quantity = 0
            status = OrderStatus.REJECTED
            idempotency_key = ""
        else:
            idempotency_key = build_idempotency_key(
                namespace="order",
                symbol=signal.symbol,
                strategy_id=signal.strategy_id,
                source_timestamp=signal.source_timestamp,
                direction=signal.direction.value,
            )
            quantity = risk_decision.position_size
            status = OrderStatus.CREATED
            reason = "Paper order candidate created for Alpaca Paper broker submission."
        return PaperOrder(
            symbol=signal.symbol,
            side=entry_side_from_direction(signal.direction),
            quantity=quantity,
            order_type="limit",
            limit_price=signal.entry_zone[0],
            stop_loss=signal.stop_loss,
            idempotency_key=idempotency_key,
            status=status,
            reason=reason,
            created_at=datetime.now(UTC),
        )

    async def submit_signal_to_paper(
        self,
        *,
        signal_id: str,
        account_equity: float | None = None,
        open_positions: int | None = None,
        daily_loss_pct: float | None = None,
        weekly_loss_pct: float,
        sector_exposure_pct: float,
        symbol_exposure_pct: float = 0.0,
        strategy_exposure_pct: float = 0.0,
        correlated_exposure_pct: float = 0.0,
        overnight_exposure_pct: float = 0.0,
        event_risk_active: bool = False,
        spread_bps: float = 0.0,
        expected_slippage_bps: float = 0.0,
        trades_today: int | None = None,
        strategy_trades_today: int | None = None,
        internal_quantity: float = 0.0,
        broker_quantity: float = 0.0,
    ) -> dict[str, Any]:
        signal_row = self.repository.signal_by_id(signal_id)
        if not signal_row:
            raise ValueError(f"Unknown signal id: {signal_id}")
        signal = _db_signal_to_trade_signal(signal_row)
        try:
            authoritative_state = self._authoritative_portfolio_state(
                signal=signal,
                environment_mode=EnvironmentMode.PAPER.value,
                broker="alpaca_paper",
            )
        except RuntimeError:
            if account_equity is None or open_positions is None or daily_loss_pct is None:
                raise
            authoritative_state = SimpleNamespace(
                account_equity=account_equity,
                open_positions=open_positions,
                daily_loss_pct=daily_loss_pct,
                trades_today=trades_today or 0,
                trades_by_strategy_today={signal.strategy_id: strategy_trades_today or 0},
            )
        account_equity = (
            authoritative_state.account_equity if account_equity is None else account_equity
        )
        open_positions = (
            authoritative_state.open_positions if open_positions is None else open_positions
        )
        daily_loss_pct = (
            authoritative_state.daily_loss_pct if daily_loss_pct is None else daily_loss_pct
        )
        trades_today = authoritative_state.trades_today if trades_today is None else trades_today
        strategy_trades_today = (
            authoritative_state.trades_by_strategy_today.get(signal.strategy_id, 0)
            if strategy_trades_today is None
            else strategy_trades_today
        )
        live_sync = await self.sync_alpaca_live()
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
        annualized_volatility = self._latest_annualized_volatility(signal.symbol)
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
            annualized_volatility=annualized_volatility,
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
        order = self._build_paper_order_candidate(
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
            broker_submit = await adapter.submit_limit_bracket_order(
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

    async def submit_signal_to_live(
        self,
        *,
        signal_id: str,
        weekly_loss_pct: float,
        sector_exposure_pct: float,
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
        live_sync = await self.sync_alpaca_live()
        if not live_sync.get("success"):
            raise RuntimeError(f"Unable to fetch authoritative live broker state: {live_sync.get('reason')}")
        authoritative_state = self._authoritative_portfolio_state(
            signal=signal,
            environment_mode=EnvironmentMode.LIVE.value,
            broker="alpaca_live",
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
        annualized_volatility = self._latest_annualized_volatility(signal.symbol)
        spread_context = self._latest_spread_context(signal.symbol)
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
            annualized_volatility=annualized_volatility,
            trades_today=trades_today,
            trades_by_strategy_today={signal.strategy_id: strategy_trades_today},
            broker_sync_ok=reconciliation.ok,
            broker_sync_reason=reconciliation.reason,
            kill_switch_active=self.repository.active_kill_switch_count() > 0,
            data_source=spread_context.get("data_source"),
            spread_note=spread_context.get("spread_note"),
            spread_is_proxy=bool(spread_context.get("spread_is_proxy", False)),
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
        with DistributedLock(redis_client_from_settings(self.settings), "live_broker_sync_lock"):
            return await LiveExecutionService(
                self.repository,
                adapter=AlpacaLiveAdapter(self.settings),
            ).submit_limit_order(
                signal=signal,
                signal_id=signal_row.id,
                risk_decision=risk_decision,
                reconciliation=reconciliation,
            )

    async def submit_internal_order_to_broker(
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
            broker_submit = await AlpacaLiveAdapter(self.settings).submit_market_order(
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
            broker_submit = await AlpacaPaperAdapter(self.settings).submit_market_order(
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

    def _latest_spread_context(self, symbol: str) -> dict[str, Any]:
        row = self.repository.session.scalar(
            select(models.SymbolFeatureSnapshot)
            .where(models.SymbolFeatureSnapshot.symbol == symbol.upper())
            .order_by(desc(models.SymbolFeatureSnapshot.source_timestamp))
            .limit(1)
        )
        if not row or not isinstance(row.snapshot, dict):
            return {}
        return {
            "data_source": row.snapshot.get("data_source"),
            "spread_note": row.snapshot.get("spread_note"),
            "spread_is_proxy": row.snapshot.get("spread_is_proxy", False),
        }

    def _latest_annualized_volatility(self, symbol: str) -> float | None:
        candles = self.repository.get_symbol_recent_candles(symbol=symbol, timeframe="1d", limit=14)
        if not candles:
            return 0.4
        ewma_true_range = calculate_ewma_true_range(candles)
        return calculate_annualized_volatility_from_ewma_true_range(
            ewma_true_range,
            current_price=float(candles[-1]["close"]),
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
