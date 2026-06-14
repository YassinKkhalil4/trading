from fastapi import APIRouter

from trading_system.app.api.routers.common import *

router = APIRouter()


@router.get("/risk/checks")
def risk_checks(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "risk_checks", lambda repo, row_limit: repo.latest_risk_checks(row_limit), limit
    )


@router.get("/risk/exposures")
def exposure_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "exposure_snapshots",
        lambda repo, row_limit: repo.latest_exposure_snapshots(row_limit),
        limit,
    )


@router.get("/broker/account-snapshots")
def broker_account_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "broker_account_snapshots",
        lambda repo, row_limit: repo.latest_broker_account_snapshots(row_limit),
        limit,
    )


@router.get("/broker/sync-logs")
def broker_sync_logs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "broker_sync_logs",
        lambda repo, row_limit: repo.latest_broker_sync_logs(row_limit),
        limit,
    )


@router.get("/execution/orders")
def execution_orders(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "orders", lambda repo, row_limit: repo.latest_orders(row_limit), limit
    )


@router.get("/execution/fills")
def execution_fills(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "fills", lambda repo, row_limit: repo.latest_fills(row_limit), limit
    )


@router.get("/execution/positions")
def execution_positions(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "positions", lambda repo, row_limit: repo.latest_positions(row_limit), limit
    )


@router.get("/execution/errors")
def execution_errors(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "execution_errors",
        lambda repo, row_limit: repo.latest_execution_errors(row_limit),
        limit,
    )


@router.post("/journal/entries")
def journal_entry_create(
    request: JournalEntryCreateBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        row = service.repository.store_journal_entry(
            symbol=request.symbol,
            strategy_id=request.strategy_id,
            entry_thesis=request.entry_thesis,
            actual_entry=request.actual_entry,
            actual_exit=request.actual_exit,
            pnl=request.pnl,
            human_notes=request.human_notes,
            mistake_tags=request.mistake_tags,
            change_reason="Manual dashboard journal entry.",
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="JOURNAL_ENTRY_CREATED",
            entity_type="journal_entry",
            entity_id=row.id,
            reason="Manual dashboard journal entry.",
            payload={"symbol": request.symbol, "source": "api"},
        )
        return {"journal_entry": model_to_dict(row)}
    finally:
        session.close()


@router.get("/journal/entries")
def journal_entries(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "journal", lambda repo, row_limit: repo.latest_journal(row_limit), limit
    )


@router.get("/decisions")
def decisions(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    return _read_rows(
        "decisions", lambda repo, row_limit: repo.latest_decisions(row_limit), limit
    )


@router.post("/execution/paper/submit-signal")
def submit_db_signal_to_paper(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _submit_db_signal_to_paper(request)


@router.post("/execution/paper/submit")
def submit_paper_signal(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _submit_db_signal_to_paper(request)


@router.post("/execution/live/submit")
def submit_live_signal(
    request: DbPaperSubmitRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.submit_signal_to_live(
            signal_id=request.signal_id,
            weekly_loss_pct=request.weekly_loss_pct,
            sector_exposure_pct=request.sector_exposure_pct,
            symbol_exposure_pct=request.symbol_exposure_pct,
            strategy_exposure_pct=request.strategy_exposure_pct,
            correlated_exposure_pct=request.correlated_exposure_pct,
            overnight_exposure_pct=request.overnight_exposure_pct,
            event_risk_active=request.event_risk_active,
            spread_bps=request.spread_bps,
            expected_slippage_bps=request.expected_slippage_bps,
            internal_quantity=request.internal_quantity,
            broker_quantity=request.broker_quantity,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/execution/live/cancel-all")
def cancel_all_live_orders(
    request: LiveEmergencyBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.cancel_all_live_orders(
            actor=principal.username, reason=request.reason
        )
    finally:
        session.close()


@router.post("/execution/live/flatten-all")
def flatten_all_live_positions(
    request: LiveEmergencyBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.flatten_all_live_positions(
            actor=principal.username, reason=request.reason
        )
    finally:
        session.close()


@router.post("/execution/orders/replace")
def replace_order(
    request: OrderReplaceBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return (
            OrderManager(service.repository)
            .request_replace_order(
                order_id=request.order_id,
                reason=request.reason,
                actor=principal.username,
                new_limit_price=request.new_limit_price,
                new_stop_loss=request.new_stop_loss,
            )
            .__dict__
        )
    finally:
        session.close()


@router.post("/execution/orders/submit-broker")
def submit_internal_order_to_broker(
    request: OrderBrokerSubmitBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.submit_internal_order_to_broker(
            order_id=request.order_id,
            actor=principal.username,
            reason=request.reason,
        )
    finally:
        session.close()


@router.post("/broker/alpaca-paper/sync")
def sync_alpaca_paper(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.sync_alpaca_paper()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="sync_alpaca_paper",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/broker/alpaca-live/sync")
def sync_alpaca_live(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.sync_alpaca_live()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="sync_alpaca_live",
            reason=result.get("reason", "Manual Alpaca live sync requested."),
            result=result,
        )
        return result
    finally:
        session.close()


@router.post("/reconciliation/fills/run-once")
def run_fill_reconciliation(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.run_fill_reconciliation_once()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_fill_reconciliation",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/risk/check-vwap-reclaim")
def risk_check_vwap_reclaim(
    request: PaperOrderRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request.scan)
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request.scan,
            decision=decision,
            signal=signal,
        )
        if not signal:
            return {
                "scanner_decision": decision.__dict__,
                "risk_decision": None,
                "scanner_result_id": scanner_result_id,
                "signal_id": signal_id,
            }
    finally:
        if not signal:
            session.close()
    if not signal:
        return {"scanner_decision": decision.__dict__, "risk_decision": None}
    risk_decision = RiskEngine().evaluate(
        signal,
        PortfolioState(
            account_equity=0.0,
            open_positions=0,
            daily_loss_pct=0.0,
            weekly_loss_pct=request.risk.weekly_loss_pct,
            sector_exposure_pct=request.risk.sector_exposure_pct,
            symbol_exposure_pct=request.risk.symbol_exposure_pct,
            strategy_exposure_pct=request.risk.strategy_exposure_pct,
            correlated_exposure_pct=request.risk.correlated_exposure_pct,
            overnight_exposure_pct=request.risk.overnight_exposure_pct,
            event_risk_active=request.risk.event_risk_active,
            spread_bps=request.risk.spread_bps,
            expected_slippage_bps=request.risk.expected_slippage_bps,
            trades_today=0,
            trades_by_strategy_today=request.risk.trades_by_strategy_today,
            kill_switch_active=request.risk.kill_switch_active,
            broker_sync_ok=request.risk.broker_sync_ok,
            broker_sync_reason=request.risk.broker_sync_reason,
        ),
    )
    try:
        risk_check_id = _persist_direct_risk_decision(
            service.repository,
            request=request,
            signal=signal,
            risk_decision=risk_decision,
            signal_id=signal_id,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__,
        "risk_decision": risk_decision.__dict__,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
        "risk_check_id": risk_check_id,
    }


@router.post("/execution/paper/submit-vwap-reclaim")
def submit_paper_order(
    request: PaperOrderRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request.scan)
    if not signal:
        raise HTTPException(status_code=422, detail=decision.reason)
    risk_decision = RiskEngine().evaluate(
        signal,
        PortfolioState(
            account_equity=0.0,
            open_positions=0,
            daily_loss_pct=0.0,
            weekly_loss_pct=request.risk.weekly_loss_pct,
            sector_exposure_pct=request.risk.sector_exposure_pct,
            symbol_exposure_pct=request.risk.symbol_exposure_pct,
            strategy_exposure_pct=request.risk.strategy_exposure_pct,
            correlated_exposure_pct=request.risk.correlated_exposure_pct,
            overnight_exposure_pct=request.risk.overnight_exposure_pct,
            event_risk_active=request.risk.event_risk_active,
            spread_bps=request.risk.spread_bps,
            expected_slippage_bps=request.risk.expected_slippage_bps,
            trades_today=0,
            trades_by_strategy_today=request.risk.trades_by_strategy_today,
            kill_switch_active=request.risk.kill_switch_active,
            broker_sync_ok=request.risk.broker_sync_ok,
            broker_sync_reason=request.risk.broker_sync_reason,
        ),
    )
    reconciliation = reconcile_positions(
        [
            PositionSnapshot(
                symbol=request.scan.symbol,
                internal_quantity=request.internal_quantity,
                broker_quantity=request.broker_quantity,
            )
        ]
    )
    order = PaperExecutionEngine().submit_limit_order(
        signal=signal,
        risk_decision=risk_decision,
        reconciliation=reconciliation,
    )
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request.scan,
            decision=decision,
            signal=signal,
            source="api:/execution/paper/submit-vwap-reclaim",
        )
        risk_check_id = _persist_direct_risk_decision(
            service.repository,
            request=request,
            signal=signal,
            risk_decision=risk_decision,
            signal_id=signal_id,
            source="api:/execution/paper/submit-vwap-reclaim",
        )
        order_row = service.repository.store_order(
            order,
            signal_id=signal_id,
            strategy_id=signal.strategy_id,
            environment_mode=EnvironmentMode.PAPER.value,
            source_timestamp=order.created_at,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__,
        "risk_decision": risk_decision.__dict__,
        "reconciliation": reconciliation.__dict__,
        "order": order.__dict__,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
        "risk_check_id": risk_check_id,
        "order_id": order_row.id,
    }
