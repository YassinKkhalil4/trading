from fastapi import APIRouter

from trading_system.app.api.routers.common import *

router = APIRouter()


@router.get("/admin/users")
def admin_users(
    _principal: AdminPrincipal = Depends(require_admin_token),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"admin_users": service.repository.list_admin_users(limit)}
    finally:
        session.close()


@router.post("/admin/users")
def admin_user_upsert(
    request: AdminUserUpsertBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required.")
    if username == principal.username and request.role != AdminRole.ADMIN:
        raise HTTPException(
            status_code=409,
            detail="Admins cannot demote their own active session user.",
        )
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.upsert_admin_user(
            username=username,
            password_hash=hash_password(request.password),
            role=request.role.value,
            reason=request.reason,
        )
        revoked_sessions = service.repository.revoke_admin_sessions_for_user(
            user_id=row.id,
            reason="Admin user password or role updated; active sessions revoked.",
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_UPSERTED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={
                "username": row.username,
                "role": row.role,
                "is_active": row.is_active,
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"admin_user": _admin_user_response(row)}
    finally:
        session.close()


@router.post("/admin/users/role")
def admin_user_role(
    request: AdminUserRoleBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    username = request.username.strip()
    if username == principal.username and request.role != AdminRole.ADMIN:
        raise HTTPException(
            status_code=409,
            detail="Admins cannot demote their own active session user.",
        )
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_admin_user_role(
            username=username,
            role=request.role.value,
            reason=request.reason,
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_ROLE_CHANGED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={"username": row.username, "role": row.role},
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@router.post("/admin/users/active")
def admin_user_active(
    request: AdminUserActiveBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    if request.username.strip() == principal.username and not request.is_active:
        raise HTTPException(
            status_code=409,
            detail="Admins cannot deactivate their own active session user.",
        )
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_admin_user_active(
            username=request.username.strip(),
            is_active=request.is_active,
            reason=request.reason,
        )
        revoked_sessions = 0
        if not row.is_active:
            revoked_sessions = service.repository.revoke_admin_sessions_for_user(
                user_id=row.id,
                reason="Admin user deactivated; active sessions revoked.",
            )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_ACTIVE_CHANGED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={
                "username": row.username,
                "is_active": row.is_active,
                "revoked_sessions": revoked_sessions,
            },
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@router.post("/admin/users/unlock")
def admin_user_unlock(
    request: AdminUserUnlockBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.clear_admin_user_lockout(
            username=request.username.strip(),
            reason=request.reason,
        )
        service.repository.store_audit_log(
            actor=principal.username,
            event_type="ADMIN_USER_UNLOCKED",
            entity_type="admin_user",
            entity_id=row.id,
            reason=request.reason,
            payload={"username": row.username},
        )
        return {"admin_user": _admin_user_response(row)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "message": "Production-gated platform. Live trading path is disabled unless every live gate passes.",
    }


@router.get("/ops/health")
def ops_health(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    settings = get_settings()
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "status": "ok",
            "environment_mode": settings.environment_mode.value,
            "live_order_path_enabled": settings.live_order_path_enabled,
            "counts": service.repository.counts(),
            "active_kill_switches": service.repository.active_kill_switch_count(),
        }
    finally:
        session.close()


@router.get("/ops/workers")
def ops_workers(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"workers": service.repository.latest_worker_heartbeats(100)}
    finally:
        session.close()


@router.get("/environment")
def environment(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    settings = get_settings()
    return {
        "mode": settings.environment_mode.value,
        "allowed_modes": [item.value for item in EnvironmentMode],
        "default_mode": EnvironmentMode.RESEARCH.value,
        "live_default": False,
        "live_order_path_enabled": settings.live_order_path_enabled,
    }


@router.get("/provider-capabilities")
def provider_capabilities(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "provider_capabilities": service.repository.list_rows(
                models.ProviderCapability, 100
            )
        }
    finally:
        session.close()


@router.get("/strategies")
def strategies(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        rows = service.repository.list_rows(models.StrategyRegistry, 100)
        for row in rows:
            row["paper_trade_allowed"] = row["status"] in {
                "PAPER_TESTING",
                "APPROVED_SMALL_SIZE",
                "APPROVED_FULL_SIZE",
            }
            row["live_trade_allowed"] = row["status"] in {
                "APPROVED_SMALL_SIZE",
                "APPROVED_FULL_SIZE",
            }
        return {"strategies": rows}
    finally:
        session.close()


@router.post("/db/bootstrap")
def bootstrap_database(
    _principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return {"counts": service.bootstrap()}
    finally:
        session.close()


@router.get("/streams/events")
def stream_events(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "stream_events",
        lambda repo, row_limit: repo.latest_stream_events(row_limit),
        limit,
    )


@router.get("/scheduler/runs")
def scheduler_runs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "scheduler_runs",
        lambda repo, row_limit: repo.latest_scheduler_runs(row_limit),
        limit,
    )


@router.get("/data/news")
def clean_news(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "clean_news", lambda repo, row_limit: repo.latest_clean_news(row_limit), limit
    )


@router.get("/data/sec-filings")
def sec_filings(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "filings", lambda repo, row_limit: repo.latest_filings(row_limit), limit
    )


@router.get("/data/quality-errors")
def data_quality_errors(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "data_quality_errors",
        lambda repo, row_limit: repo.latest_data_quality_errors(row_limit),
        limit,
    )


@router.get("/data/missing-candle-gaps")
def missing_candle_gaps(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "missing_candle_gaps",
        lambda repo, row_limit: repo.latest_missing_candle_gaps(row_limit),
        limit,
    )



@router.get("/reviews/weekly")
def weekly_reviews(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return _read_rows(
        "weekly_reviews",
        lambda repo, row_limit: repo.latest_weekly_reviews(row_limit),
        limit,
    )


@router.get("/learning/recommendations")
def learning_recommendations(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "strategy_recommendations",
        lambda repo, row_limit: repo.latest_strategy_recommendations(row_limit),
        limit,
    )


@router.get("/backtests/reports")
def backtest_reports(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    return _read_rows(
        "backtest_reports",
        lambda repo, row_limit: repo.latest_backtest_reports(row_limit),
        limit,
    )


@router.get("/kill-switches")
def kill_switches(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "kill_switches",
        lambda repo, row_limit: repo.latest_kill_switches(row_limit),
        limit,
    )


@router.get("/api/v1/audit/logs")
@router.get("/audit/logs")
def audit_logs(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    return _read_rows(
        "audit_logs", lambda repo, row_limit: repo.latest_audit_logs(row_limit), limit
    )


@router.post("/data/repair-missing-candles")
def repair_missing_candles(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.repair_missing_candles(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="repair_missing_candles",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/backtests/run")
def backtests_run(
    request: BacktestRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_backtest(
            strategy_id=request.strategy_id,
            symbol=request.symbol,
            provider=request.provider,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="backtests_run",
            reason="Manual backtest run requested.",
            payload={
                "strategy_id": request.strategy_id,
                "symbol": request.symbol,
                "provider": request.provider,
            },
            result=result,
        )
        return result
    finally:
        session.close()


@router.post("/collect/yahoo")
def collect_yahoo(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        symbols = request.symbols or service.repository.active_symbols()
        results = []
        for symbol in symbols:
            row = service.repository.add_or_activate_symbol(
                symbol, reason="Activated for API collection."
            )
            if request.symbols:
                _store_symbol_config_audit(
                    service.repository,
                    actor=principal.username,
                    event_type="SYMBOL_ACTIVATED_FOR_COLLECTION",
                    row=row,
                    reason="Activated for API collection.",
                    payload={"source": "api", "collector": "yahoo"},
                )
            results.append(service.collect_symbol(symbol).__dict__)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_yahoo",
            reason="Manual Yahoo research collection requested.",
            payload={"symbols": symbols},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": results}
    finally:
        session.close()


@router.post("/collect/alpaca-bars")
def collect_alpaca_bars(
    request: AlpacaBarsCollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        symbols = request.symbols or service.repository.active_symbols()
        results = []
        from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector

        collector = AlpacaBarsCollector(service.repository, service.settings)
        for symbol in symbols:
            row = service.repository.add_or_activate_symbol(
                symbol,
                reason="Activated for Alpaca bar collection.",
            )
            if request.symbols:
                _store_symbol_config_audit(
                    service.repository,
                    actor=principal.username,
                    event_type="SYMBOL_ACTIVATED_FOR_COLLECTION",
                    row=row,
                    reason="Activated for Alpaca bar collection.",
                    payload={"source": "api", "collector": "alpaca_bars"},
                )
            results.append(
                collector.collect(symbol, timeframe=request.timeframe).__dict__
            )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_alpaca_bars",
            reason="Manual Alpaca bar collection requested.",
            payload={"symbols": symbols, "timeframe": request.timeframe},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": results}
    finally:
        session.close()


@router.post("/streams/alpaca/market-data/run-once")
async def run_alpaca_market_data_stream(
    request: AlpacaStreamRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = await service.run_alpaca_market_data_stream(
            symbols=request.symbols,
            channels=request.channels,
            max_messages=request.max_messages,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_alpaca_market_data_stream",
            reason=result.reason,
            payload={
                "symbols": request.symbols,
                "channels": request.channels,
                "max_messages": request.max_messages,
            },
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/collect/news")
def collect_news(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.collect_news(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_news",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/collect/sec")
def collect_sec(
    request: SecCollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.collect_sec_filings(
            request.symbols,
            max_filings_per_symbol=request.max_filings_per_symbol,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="collect_sec",
            reason=result.reason,
            payload={
                "symbols": request.symbols,
                "max_filings_per_symbol": request.max_filings_per_symbol,
            },
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/scheduler/run-once")
def run_scheduler_once(
    request: SchedulerRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.run_scheduled_job(
            request.job_name,
            symbols=request.symbols,
            actor=principal.username,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="run_scheduled_job",
            reason=result.reason,
            payload={"job_name": request.job_name, "symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/live-readiness/report")
def live_readiness_report(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _live_readiness_report(actor=principal.username)


@router.post("/live-readiness/run")
def live_readiness_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    return _live_readiness_report(actor=principal.username)


@router.get("/live-readiness/reports")
def live_readiness_reports(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "live_readiness_reports": service.repository.latest_live_readiness_reports(
                limit
            )
        }
    finally:
        session.close()


@router.get("/live-readiness/detail")
def live_readiness_detail(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        detail = LiveReadinessService(
            service.repository, service.settings
        ).get_detail_report()
        return detail.to_dict()
    finally:
        session.close()


@router.post("/kill-switches/activate")
def kill_switch_activate(
    request: KillSwitchActivateBody,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.activate_kill_switch(
            event_type=request.event_type,
            reason=request.reason,
            payload=request.payload,
            actor=principal.username,
        ).__dict__
    finally:
        session.close()


@router.post("/kill-switches/resolve")
def kill_switch_resolve(
    request: KillSwitchResolveBody,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> dict:
    session, service = _runtime()
    try:
        return service.resolve_kill_switch(
            event_id=request.event_id,
            reason=request.reason,
            actor=principal.username,
        ).__dict__
    finally:
        session.close()


@router.post("/provider-health/run")
def provider_health_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_provider_health()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="provider_health_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/features/run")
def features_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_features(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="features_run",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/regime/snapshot/run")
def regime_snapshot_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_market_regime()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="regime_snapshot_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/catalysts/score/run")
def catalysts_score_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_catalysts(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="catalysts_score_run",
            reason=result.reason,
            payload={"symbols": request.symbols},
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/scanners/production/run", status_code=status.HTTP_202_ACCEPTED)
def production_scanners_run(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    task = run_production_scanners.delay(request.symbols, actor=principal.username)
    return {
        "accepted": True,
        "task_id": task.id,
        "reason": "Production scanner execution queued in Celery.",
    }


@router.post("/monitor/trades/run-once")
def trade_monitor_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_trade_monitor()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="trade_monitor_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/reviews/weekly/run")
def weekly_reviews_run(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        result = service.run_learning_review()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="weekly_reviews_run",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()
