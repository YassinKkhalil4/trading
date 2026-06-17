from fastapi import APIRouter

from trading_system.app.api.routers.common import *

router = APIRouter()


@router.get("/calendar/session")
def calendar_session(
    timestamp: datetime,
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    info = get_session(timestamp)
    return {
        "status": info.status.value,
        "session_date": info.session_date.isoformat(),
        "open_at": info.open_at.isoformat() if info.open_at else None,
        "close_at": info.close_at.isoformat() if info.close_at else None,
        "reason": info.reason,
    }


@router.get("/calendar/opening-range")
def calendar_opening_range(
    session_date: datetime,
    minutes: int = 15,
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    start, end = opening_range_window(session_date.date(), minutes=minutes)
    return {"start": start.isoformat(), "end": end.isoformat(), "minutes": minutes}


@router.get("/universe")
def universe(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"active_symbols": service.repository.active_symbols()}
    finally:
        session.close()


@router.get("/market/clean-candles")
def market_clean_candles(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
    symbol: str | None = Query(default=None),
) -> dict:
    if not symbol:
        return _read_rows(
            "clean_candles",
            lambda repo, row_limit: repo.latest_clean_candles(row_limit),
            limit,
        )
    session, service = _runtime()
    try:
        frame = service.repository.clean_candles_df(symbol, limit=limit)
        rows = (
            []
            if frame is None or frame.empty
            else jsonable_encoder(frame.to_dict(orient="records"))
        )
        return {"clean_candles": rows}
    finally:
        session.close()


@router.get("/features/latest")
def latest_features(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "features", lambda repo, row_limit: repo.latest_features(row_limit), limit
    )


@router.get("/features/daily")
def daily_features(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "daily_features",
        lambda repo, row_limit: repo.latest_daily_features(row_limit),
        limit,
    )


@router.get("/api/v1/market/regime/latest")
def latest_regime_snapshot_v1(
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.latest_market_regime_snapshot()
        return {"regime": jsonable_encoder(model_to_dict(row)) if row else None}
    finally:
        session.close()


@router.get("/regime/snapshots")
def regime_snapshots(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "regime_snapshots",
        lambda repo, row_limit: repo.latest_regime_snapshots(row_limit),
        limit,
    )


@router.get("/catalysts/events")
def catalyst_events(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "events", lambda repo, row_limit: repo.latest_events(row_limit), limit
    )


@router.get("/catalysts/scores")
def catalyst_scores(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "catalysts", lambda repo, row_limit: repo.latest_catalysts(row_limit), limit
    )


@router.get("/scanners/results")
def scanner_results(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "scanner_results",
        lambda repo, row_limit: repo.latest_scanner_results(row_limit),
        limit,
    )


@router.get("/rankings/recent")
def recent_rankings(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        ranking_service = OpportunityRankingService(service.repository, get_settings())
        ranked = ranking_service.rank_recent_accepted(limit)
        expectancy_view = ExpectancyService(service.repository).load()
        current_regime = latest_market_regime(service.repository)
        rankings = []
        for item in ranked:
            payload = _ranking_to_dict(item)
            payload["expectancy"] = stats_to_dict(
                expectancy_view.match(
                    strategy_id=item.strategy_id,
                    symbol=item.symbol,
                    regime=current_regime,
                )
            )
            rankings.append(payload)
        return {"rankings": rankings}
    finally:
        session.close()


@router.get("/expectancy/summary")
def expectancy_summary(
    _principal: AdminPrincipal = Depends(require_principal),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        view = ExpectancyService(service.repository).load(start=start, end=end)
        summary = view.summary()
        return {
            "overall": stats_to_dict(summary["overall"]),
            "by_strategy": _stats_bucket_dict(summary.get("by_strategy", {})),
            "by_symbol": _stats_bucket_dict(summary.get("by_symbol", {})),
            "by_sector": _stats_bucket_dict(summary.get("by_sector", {})),
            "by_regime": _stats_bucket_dict(summary.get("by_regime", {})),
            "by_market_regime": _stats_bucket_dict(summary.get("by_market_regime", {})),
            "by_time_of_day": _stats_bucket_dict(summary.get("by_time_of_day", {})),
            "by_relative_volume_bucket": _stats_bucket_dict(
                summary.get("by_relative_volume_bucket", {})
            ),
            "by_catalyst_type": _stats_bucket_dict(summary.get("by_catalyst_type", {})),
            "by_spread_bucket": _stats_bucket_dict(summary.get("by_spread_bucket", {})),
            "by_volatility_bucket": _stats_bucket_dict(
                summary.get("by_volatility_bucket", {})
            ),
        }
    finally:
        session.close()


@router.get("/alpha/opportunity-scores")
def alpha_opportunity_scores(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "opportunity_scores",
        lambda repo, row_limit: repo.latest_opportunity_scores(row_limit),
        limit,
    )


@router.get("/alpha/opportunity-scores/{symbol}")
def alpha_opportunity_scores_by_symbol(
    symbol: str,
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "symbol": symbol.upper(),
            "scores": service.repository.latest_opportunity_scores_for_symbol(
                symbol, limit
            ),
        }
    finally:
        session.close()


@router.get("/alpha/opportunity-scores/{score_id}/explanation")
def alpha_opportunity_score_explanation(
    score_id: str,
    _principal: AdminPrincipal = Depends(require_principal),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = session.get(models.OpportunityScore, score_id)
        if not row:
            raise HTTPException(status_code=404, detail="Opportunity score not found.")
        return {
            "score": model_to_dict(row),
            "components": service.repository.opportunity_score_components(score_id),
        }
    finally:
        session.close()


@router.get("/alpha/expectancy")
def alpha_expectancy(
    _principal: AdminPrincipal = Depends(require_principal),
    strategy_id: str | None = None,
    setup_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    rows = _read_rows(
        "expectancy",
        lambda repo, row_limit: repo.latest_expectancy_snapshots(row_limit),
        limit,
    )
    filtered = rows["expectancy"]
    if strategy_id:
        filtered = [row for row in filtered if row.get("strategy_id") == strategy_id]
    if setup_type:
        filtered = [row for row in filtered if row.get("setup_type") == setup_type]
    return {"expectancy": filtered}


@router.get("/alpha/sector-leadership")
def alpha_sector_leadership(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {
            "sectors": service.repository.latest_sector_strength(limit),
            "symbols": service.repository.latest_symbol_relative_strength(limit),
        }
    finally:
        session.close()


@router.get("/alpha/candidates")
def alpha_candidates(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        return {"candidates": service.repository.latest_opportunity_scores(limit)}
    finally:
        session.close()


@router.get("/alpha/rejections")
def alpha_rejections(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "rejections",
        lambda repo, row_limit: repo.latest_alpha_rejections(row_limit),
        limit,
    )


@router.post("/alpha/scanners/run", status_code=status.HTTP_202_ACCEPTED)
def alpha_scanner_run(
    request: AlphaScannerRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    task = run_alpha_strategy_scanner.delay(
        request.strategy_id, symbols=request.symbols, actor=principal.username
    )
    return {
        "accepted": True,
        "task_id": task.id,
        "reason": "Alpha scanner execution queued in Celery.",
    }


@router.post("/alpha/expectancy/refresh")
def alpha_expectancy_refresh(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = AlphaExpectancyRefreshService(service.repository).refresh()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="alpha_expectancy_refresh",
            reason=result.reason,
            result=result,
        )
        return result.__dict__
    finally:
        session.close()


@router.post("/alpha/regime/refresh")
def alpha_regime_refresh(
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = service.run_market_regime()
        leadership = SectorLeadershipService(service.repository).refresh()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="alpha_regime_refresh",
            reason=result.reason,
            result={"regime": result.__dict__, "leadership": leadership.__dict__},
        )
        return {"regime": result.__dict__, "leadership": leadership.__dict__}
    finally:
        session.close()


@router.get("/alpha/point-in-time-universe")
def alpha_point_in_time_universe(
    _principal: AdminPrincipal = Depends(require_principal),
    as_of: datetime | None = None,
    universe_name: str = "tradable_us_equities",
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        timestamp = as_of or datetime.now(UTC)
        return {
            "universe_name": universe_name,
            "as_of": timestamp,
            "members": service.repository.point_in_time_universe(
                as_of=timestamp, universe_name=universe_name
            ),
        }
    finally:
        session.close()


@router.post("/alpha/point-in-time-universe/refresh")
def alpha_point_in_time_universe_refresh(
    request: PointInTimeUniverseRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = PointInTimeUniverseService(
            service.repository
        ).snapshot_current_universe(
            universe_name=request.universe_name,
            as_of=request.as_of,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="alpha_point_in_time_universe_refresh",
            reason=result.reason,
            payload=request.model_dump(),
            result={"records_created": result.records_created},
        )
        return result.__dict__
    finally:
        session.close()


@router.get("/alpha/short-interest")
def alpha_short_interest(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "short_interest",
        lambda repo, row_limit: repo.latest_short_interest_snapshots(row_limit),
        limit,
    )


@router.post("/alpha/short-interest/refresh")
def alpha_short_interest_refresh(
    request: AlphaRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = ShortInterestService(
            service.repository
        ).refresh_from_universe_payloads(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="alpha_short_interest_refresh",
            reason=result.reason,
            payload=request.model_dump(),
            result={"records_created": result.records_created},
        )
        return result.__dict__
    finally:
        session.close()


@router.get("/alpha/options-intelligence")
def alpha_options_intelligence(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "options_intelligence",
        lambda repo, row_limit: repo.latest_options_intelligence_snapshots(row_limit),
        limit,
    )


@router.post("/alpha/options-intelligence/refresh")
def alpha_options_intelligence_refresh(
    request: AlphaRunRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = OptionsIntelligenceService(
            service.repository
        ).refresh_from_universe_payloads(request.symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="alpha_options_intelligence_refresh",
            reason=result.reason,
            payload=request.model_dump(),
            result={"records_created": result.records_created},
        )
        return result.__dict__
    finally:
        session.close()



@router.get("/alpha/strategies")
def alpha_strategies(_principal: AdminPrincipal = Depends(require_principal)) -> dict:
    return {"strategies": sorted(ALPHA_STRATEGIES)}


@router.get("/signals")
def signals(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "signals", lambda repo, row_limit: repo.latest_signals(row_limit), limit
    )



@router.get("/providers/health")
def provider_health(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "provider_health",
        lambda repo, row_limit: repo.latest_provider_health(row_limit),
        limit,
    )


@router.get("/providers/rate-limits")
def provider_rate_limits(
    _principal: AdminPrincipal = Depends(require_principal),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return _read_rows(
        "provider_rate_limits",
        lambda repo, row_limit: repo.latest_provider_rate_limits(row_limit),
        limit,
    )


@router.post("/symbols/activate")
def activate_symbol(
    request: SymbolRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.add_or_activate_symbol(
            request.symbol,
            name=request.name,
            sector=request.sector,
            reason=request.reason,
        )
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_ACTIVATED",
            row=row,
            reason=request.reason,
            payload={"source": "api", "name": request.name},
        )
        return {
            "symbol": row.symbol,
            "is_active": row.is_active,
            "reason": row.change_reason,
        }
    finally:
        session.close()


@router.post("/symbols/deactivate")
def deactivate_symbol(
    request: SymbolDeactivateRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.deactivate_symbol(
            request.symbol, reason=request.reason
        )
        if not row:
            raise HTTPException(status_code=404, detail="Symbol not found.")
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_DEACTIVATED",
            row=row,
            reason=request.reason,
            payload={"source": "api"},
        )
        return {
            "symbol": row.symbol,
            "is_active": row.is_active,
            "reason": row.change_reason,
        }
    finally:
        session.close()


@router.post("/symbols/tradability")
def update_symbol_tradability(
    request: SymbolTradabilityRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        row = service.repository.set_symbol_tradability(
            request.symbol,
            is_tradable=request.is_tradable,
            reason=request.reason,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Symbol not found.")
        _store_symbol_config_audit(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_TRADABILITY_CHANGED",
            row=row,
            reason=request.reason,
            payload={"source": "api"},
        )
        return {
            "symbol": row.symbol,
            "is_tradable": row.is_tradable,
            "tradability_reason": row.tradability_reason,
        }
    finally:
        session.close()


@router.post("/universe/refresh")
def universe_refresh(
    request: CollectRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        return service.refresh_universe(request.symbols).__dict__
    finally:
        session.close()


@router.post("/scan/watchlist")
def scan_watchlist(
    request: CollectRequest,
    principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        for symbol in request.symbols or []:
            row = service.repository.add_or_activate_symbol(
                symbol, reason="Activated for API scan."
            )
            _store_symbol_config_audit(
                service.repository,
                actor=principal.username,
                event_type="SYMBOL_ACTIVATED_FOR_SCAN",
                row=row,
                reason="Activated for API scan.",
                payload={"source": "api"},
            )
        results = service.run_watchlist_scan(collect_first=request.collect_first)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="scan_watchlist",
            reason="Manual watchlist scan requested.",
            payload={"symbols": request.symbols or []},
            result={"success": True, "result_count": len(results)},
        )
        return {"results": [item.__dict__ for item in results]}
    finally:
        session.close()


@router.post("/scanners/vwap-reclaim")
def scan_vwap_reclaim(
    request: VwapReclaimScanRequest,
    _principal: AdminPrincipal = Depends(require_trader_or_admin),
) -> dict:
    decision, signal = _scan_to_signal(request)
    session, service = _runtime()
    try:
        scanner_result_id, signal_id = _persist_direct_scan_decision(
            service.repository,
            request=request,
            decision=decision,
            signal=signal,
        )
    finally:
        session.close()
    return {
        "scanner_decision": decision.__dict__,
        "signal": signal.__dict__ if signal else None,
        "scanner_result_id": scanner_result_id,
        "signal_id": signal_id,
    }
