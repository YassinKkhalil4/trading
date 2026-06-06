from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from trading_system.app.core.config import get_settings
from trading_system.app.core.enums import AdminRole, EnvironmentMode
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import SessionLocal
from trading_system.app.security.auth import AdminPrincipal, AuthService, hash_password
from trading_system.app.services.runtime import TradingRuntimeService


st.set_page_config(page_title="Trading Intelligence", layout="wide")


def _service() -> TradingRuntimeService:
    session = SessionLocal()
    repo = TradingRepository(session)
    return TradingRuntimeService(repo)


def _authenticate_dashboard_token(token: str) -> AdminPrincipal | None:
    session = SessionLocal()
    try:
        repo = TradingRepository(session)
        return AuthService(repo, settings).authenticate_token(token)
    finally:
        session.close()


def _login_dashboard(username: str, password: str):
    session = SessionLocal()
    try:
        repo = TradingRepository(session)
        TradingRuntimeService(repo, settings=settings).bootstrap()
        return AuthService(repo, settings).login(username, password)
    finally:
        session.close()


def _logout_dashboard(token: str, actor: str) -> None:
    session = SessionLocal()
    try:
        repo = TradingRepository(session)
        AuthService(repo, settings).logout(token, actor=actor)
    finally:
        session.close()


def _table(rows: list[dict[str, Any]], *, label: str, height: int = 360) -> None:
    if not rows:
        st.info(f"No {label} records in the database yet.")
        return
    frame = pd.DataFrame(rows)
    st.dataframe(frame, width="stretch", height=height)


def _compact_dict(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {val}" for key, val in value.items())
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _audit_symbol_config(
    repo: TradingRepository,
    *,
    actor: str,
    event_type: str,
    symbol_row: Any,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> None:
    repo.store_audit_log(
        actor=actor,
        event_type=event_type,
        entity_type="symbol_universe",
        entity_id=symbol_row.id,
        reason=reason,
        payload={
            "symbol": symbol_row.symbol,
            "is_active": symbol_row.is_active,
            "is_tradable": symbol_row.is_tradable,
            "sector": symbol_row.sector,
            "source": "dashboard",
            **(payload or {}),
        },
    )


def _operation_result_summary(result: Any) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    if not isinstance(raw, dict):
        return {"result_type": type(result).__name__}
    summary_keys = [
        "success",
        "reason",
        "overall_status",
        "live_allowed",
        "report_id",
        "blockers",
        "warnings",
        "job_name",
        "result_count",
        "version",
    ]
    summary = {key: raw[key] for key in summary_keys if key in raw}
    summary["result_type"] = type(result).__name__
    return summary


def _audit_manual_operation(
    repo: TradingRepository,
    *,
    actor: str,
    operation: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    result: Any = None,
) -> None:
    repo.store_audit_log(
        actor=actor,
        event_type="MANUAL_OPERATION_RUN",
        entity_type="manual_operation",
        entity_id=operation,
        reason=reason,
        payload={
            "operation": operation,
            "request": payload or {},
            "result": _operation_result_summary(result) if result is not None else {},
        },
    )


settings = get_settings()

dashboard_token = st.session_state.get("admin_token")
principal = _authenticate_dashboard_token(dashboard_token) if dashboard_token else None
if not principal:
    st.title("Trading Intelligence")
    st.caption("Admin access is required.")
    with st.form("admin_login"):
        username = st.text_input("Username", value=settings.admin_username)
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
        if submitted:
            try:
                result = _login_dashboard(username, password)
            except Exception as exc:
                st.error(f"Authentication service is not ready: {exc}")
                st.stop()
            if result.authenticated and result.token:
                st.session_state["admin_token"] = result.token
                st.session_state["admin_username"] = result.username
                st.session_state["admin_role"] = result.role
                st.rerun()
            st.error(result.reason)
    st.stop()

can_trade = principal.role in {AdminRole.ADMIN.value, AdminRole.TRADER.value}
can_admin = principal.role == AdminRole.ADMIN.value

service = _service()

try:
    service.bootstrap()
    snapshot = service.dashboard_snapshot()
except Exception as exc:
    st.error(f"Database is not ready: {exc}")
    st.stop()

st.title("Autonomous Trading Intelligence")
st.caption("Database-backed research and paper-trading console. Live execution remains disabled.")

with st.sidebar:
    st.header("Controls")
    st.write(f"Signed in as {principal.username} ({principal.role})")
    if st.button("Sign out", width="stretch"):
        _logout_dashboard(dashboard_token or "", principal.username)
        st.session_state.pop("admin_token", None)
        st.session_state.pop("admin_username", None)
        st.session_state.pop("admin_role", None)
        st.rerun()
    st.metric("Environment", settings.environment_mode.value)
    st.metric("Live order path", "disabled")
    st.metric("Refresh seconds", settings.dashboard_refresh_seconds)
    auto_refresh = st.checkbox("Auto-refresh", value=False)

    if st.button("Initialize / Seed Database", width="stretch", disabled=not can_admin):
        counts = service.bootstrap()
        st.success(f"Database ready: {counts}")
        st.rerun()

    st.divider()
    st.subheader("Universe")
    active_symbols = snapshot["active_symbols"]
    st.write(", ".join(active_symbols) if active_symbols else "No active symbols.")
    new_symbol = st.text_input("Add/activate symbol", placeholder="AAPL")
    if st.button("Add Symbol", width="stretch", disabled=not can_trade or not new_symbol.strip()):
        reason = "Added from dashboard."
        row = service.repository.add_or_activate_symbol(new_symbol, reason=reason)
        _audit_symbol_config(
            service.repository,
            actor=principal.username,
            event_type="SYMBOL_ACTIVATED",
            symbol_row=row,
            reason=reason,
        )
        st.rerun()
    if active_symbols:
        deactivate_symbol = st.selectbox("Deactivate symbol", active_symbols)
        deactivate_reason = st.text_input("Deactivate reason", value="Deactivated from dashboard.")
        if st.button(
            "Deactivate Symbol",
            width="stretch",
            disabled=not can_trade or not deactivate_reason.strip(),
        ):
            row = service.repository.deactivate_symbol(deactivate_symbol, reason=deactivate_reason)
            if row:
                _audit_symbol_config(
                    service.repository,
                    actor=principal.username,
                    event_type="SYMBOL_DEACTIVATED",
                    symbol_row=row,
                    reason=deactivate_reason,
                )
            st.rerun()

    symbols_csv = st.text_area(
        "Symbols for next collection/scan",
        value=", ".join(active_symbols),
        help="The dashboard will use these active real symbols. It will not create fake market rows.",
    )
    selected_symbols = [item.strip().upper() for item in symbols_csv.split(",") if item.strip()]

    if st.button("Collect Real Market Data", width="stretch", disabled=not can_trade or not selected_symbols):
        results = []
        for symbol in selected_symbols:
            reason = "Activated for dashboard collection."
            row = service.repository.add_or_activate_symbol(symbol, reason=reason)
            _audit_symbol_config(
                service.repository,
                actor=principal.username,
                event_type="SYMBOL_ACTIVATED_FOR_COLLECTION",
                symbol_row=row,
                reason=reason,
                payload={"collector": "primary"},
            )
            results.append(service.collect_symbol_primary(symbol).__dict__)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_collect_real_market_data",
            reason="Manual dashboard market-data collection requested.",
            payload={"symbols": selected_symbols},
            result={"success": True, "result_count": len(results)},
        )
        st.session_state["last_collect_results"] = results
        st.rerun()

    collect_before_scan = st.checkbox("Collect before scanning", value=True)
    if st.button("Run VWAP Scan Cycle", width="stretch", disabled=not can_trade or not selected_symbols):
        for symbol in selected_symbols:
            reason = "Activated for dashboard scan."
            row = service.repository.add_or_activate_symbol(symbol, reason=reason)
            _audit_symbol_config(
                service.repository,
                actor=principal.username,
                event_type="SYMBOL_ACTIVATED_FOR_SCAN",
                symbol_row=row,
                reason=reason,
            )
        results = service.run_watchlist_scan(collect_first=collect_before_scan)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_vwap_scan_cycle",
            reason="Manual dashboard VWAP scan cycle requested.",
            payload={"symbols": selected_symbols, "collect_first": collect_before_scan},
            result={"success": True, "result_count": len(results)},
        )
        st.session_state["last_scan_results"] = [item.__dict__ for item in results]
        st.rerun()

    st.divider()
    if st.button("Sync Alpaca Paper", width="stretch", disabled=not can_trade):
        result = service.sync_alpaca_paper()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_sync_alpaca_paper",
            reason=result.reason,
            result=result,
        )
        st.session_state["last_alpaca_sync"] = result.__dict__
        st.rerun()

    if st.button("Reconcile Fills", width="stretch", disabled=not can_trade):
        result = service.run_fill_reconciliation_once()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_reconcile_fills",
            reason=result.reason,
            result=result,
        )
        st.session_state["last_fill_reconciliation"] = result.__dict__
        st.rerun()

    if st.button("Run Alpaca Stream Batch", width="stretch", disabled=not can_trade or not selected_symbols):
        result = asyncio.run(
            service.run_alpaca_market_data_stream(symbols=selected_symbols, max_messages=25)
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_alpaca_stream_batch",
            reason=result.reason,
            payload={"symbols": selected_symbols, "max_messages": 25},
            result=result,
        )
        st.session_state["last_stream_result"] = result.__dict__
        st.rerun()

    st.divider()
    st.subheader("Catalyst Collectors")
    if st.button("Collect News", width="stretch", disabled=not can_trade or not selected_symbols):
        result = service.collect_news(selected_symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_collect_news",
            reason=result.reason,
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_news_collect"] = result.__dict__
        st.rerun()

    if st.button("Collect SEC Filings", width="stretch", disabled=not can_trade or not selected_symbols):
        result = service.collect_sec_filings(selected_symbols, max_filings_per_symbol=10)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_collect_sec_filings",
            reason=result.reason,
            payload={"symbols": selected_symbols, "max_filings_per_symbol": 10},
            result=result,
        )
        st.session_state["last_sec_collect"] = result.__dict__
        st.rerun()

    if st.button("Run Features + Regime + Catalysts", width="stretch", disabled=not can_trade or not selected_symbols):
        feature_result = service.run_features(selected_symbols)
        regime_result = service.run_market_regime()
        catalyst_result = service.run_catalysts(selected_symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_features_regime_catalysts",
            reason="Manual dashboard feature, regime, and catalyst run requested.",
            payload={"symbols": selected_symbols},
            result={
                "success": True,
                "feature": feature_result.__dict__,
                "regime": regime_result.__dict__,
                "catalyst": catalyst_result.__dict__,
            },
        )
        st.session_state["last_feature_result"] = feature_result.__dict__
        st.session_state["last_regime_result"] = regime_result.__dict__
        st.session_state["last_catalyst_result"] = catalyst_result.__dict__
        st.rerun()

    if st.button("Run Production Scanners", width="stretch", disabled=not can_trade or not selected_symbols):
        result = service.run_production_scanners(selected_symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_production_scanners",
            reason=result.reason,
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_production_scanners"] = result.__dict__
        st.rerun()

    if st.button("Run Monitor + Reviews + Learning", width="stretch", disabled=not can_trade):
        monitor_result = service.run_trade_monitor()
        reviews_result = service.run_reviews()
        learning_result = service.run_learning_review()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_monitor_reviews_learning",
            reason="Manual dashboard monitor, review, and learning run requested.",
            result={
                "success": True,
                "monitor": monitor_result.__dict__,
                "reviews": reviews_result.__dict__,
                "learning": learning_result.__dict__,
            },
        )
        st.session_state["last_trade_monitor"] = monitor_result.__dict__
        st.session_state["last_reviews"] = reviews_result.__dict__
        st.session_state["last_learning"] = learning_result.__dict__
        st.rerun()

    if st.button("Refresh Provider Health", width="stretch", disabled=not can_trade):
        result = service.run_provider_health()
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_refresh_provider_health",
            reason=result.reason,
            result=result,
        )
        st.session_state["last_provider_health"] = result.__dict__
        st.rerun()

    if st.button("Refresh Liquid Universe", width="stretch", disabled=not can_trade or not selected_symbols):
        result = service.refresh_universe(selected_symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_refresh_liquid_universe",
            reason=result.reason,
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_universe_refresh"] = result.__dict__
        st.rerun()

    if st.button("Repair Missing Candles", width="stretch", disabled=not can_trade or not selected_symbols):
        result = service.repair_missing_candles(selected_symbols)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_repair_missing_candles",
            reason=result.reason,
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_missing_candle_repair"] = result.__dict__
        st.rerun()

    scheduler_job = st.selectbox(
        "Scheduled job",
        [
            "market_data",
            "features",
            "regime",
            "news",
            "sec",
            "catalysts",
            "production_scanners",
            "provider_health",
            "universe",
            "missing_candle_repair",
            "live_readiness",
            "fill_reconciliation",
            "trade_monitor",
            "reviews",
            "learning",
            "all",
        ],
    )
    if st.button("Run Scheduled Job", width="stretch", disabled=not can_trade):
        result = service.run_scheduled_job(
            scheduler_job,
            symbols=selected_symbols,
            actor=principal.username,
        )
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_run_scheduled_job",
            reason=result.reason,
            payload={"job_name": scheduler_job, "symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_scheduler_result"] = result.__dict__
        st.rerun()

    st.divider()
    if st.button("Generate Live Readiness Report", width="stretch", disabled=not can_trade):
        result = service.generate_live_readiness_report(actor=principal.username)
        _audit_manual_operation(
            service.repository,
            actor=principal.username,
            operation="dashboard_generate_live_readiness_report",
            reason=result.reason,
            result=result,
        )
        st.session_state["last_live_readiness"] = result.__dict__
        st.rerun()

    st.subheader("Emergency Controls")
    kill_reason = st.text_input("Kill switch reason", value="Manual dashboard activation.")
    if st.button("Activate Global Kill Switch", width="stretch", disabled=not can_trade):
        result = service.activate_kill_switch(
            event_type="MANUAL_GLOBAL",
            reason=kill_reason,
            payload={"source": "dashboard"},
        )
        st.session_state["last_kill_switch"] = result.__dict__
        st.rerun()

counts = snapshot["counts"]
metrics = st.columns(10)
metrics[0].metric("Symbols", counts["symbols"])
metrics[1].metric("Candles", counts["clean_candles"])
metrics[2].metric("Streams", counts["stream_events"])
metrics[3].metric("News", counts["clean_news"])
metrics[4].metric("Filings", counts["filings"])
metrics[5].metric("Signals", counts["signals"])
metrics[6].metric("Risk Checks", counts["risk_checks"])
metrics[7].metric("Orders", counts["orders"])
metrics[8].metric("Fills", counts["fills"])
metrics[9].metric("Journal", counts["journal_entries"])

if settings.environment_mode == EnvironmentMode.LIVE and not settings.live_order_path_enabled:
    st.error("Live mode is selected, but live order gates are not fully enabled.")

if "last_collect_results" in st.session_state:
    with st.expander("Last market data collection result", expanded=True):
        _table(st.session_state["last_collect_results"], label="collection result", height=180)

if "last_scan_results" in st.session_state:
    with st.expander("Last scan cycle result", expanded=True):
        rows = []
        for row in st.session_state["last_scan_results"]:
            rows.append(
                {
                    "symbol": row["symbol"],
                    "candles_seen": row["collected"].candles_seen if row.get("collected") else None,
                    "scanner_result_id": row["scanner_result_id"],
                    "signal_id": row["signal_id"],
                    "thesis_id": row["thesis_id"],
                    "reason": row["reason"],
                }
            )
        _table(rows, label="scan result", height=180)

if "last_alpaca_sync" in st.session_state:
    with st.expander("Last Alpaca paper sync result", expanded=True):
        st.json(st.session_state["last_alpaca_sync"])

for state_key, title in [
    ("last_fill_reconciliation", "Last fill reconciliation result"),
    ("last_stream_result", "Last Alpaca stream batch result"),
    ("last_news_collect", "Last news collection result"),
    ("last_sec_collect", "Last SEC collection result"),
    ("last_feature_result", "Last feature result"),
    ("last_regime_result", "Last regime result"),
    ("last_catalyst_result", "Last catalyst result"),
    ("last_production_scanners", "Last production scanner result"),
    ("last_trade_monitor", "Last trade monitor result"),
    ("last_reviews", "Last trade review result"),
    ("last_learning", "Last learning review result"),
    ("last_provider_health", "Last provider health result"),
    ("last_universe_refresh", "Last universe refresh result"),
    ("last_missing_candle_repair", "Last missing candle repair result"),
    ("last_scheduler_result", "Last scheduled job result"),
    ("last_live_readiness", "Last live-readiness report"),
    ("last_live_approval_action", "Last live approval action"),
    ("last_admin_user_action", "Last admin user action"),
    ("last_kill_switch", "Last kill switch action"),
]:
    if state_key in st.session_state:
        with st.expander(title, expanded=True):
            st.json(st.session_state[state_key])

tabs = st.tabs(
    [
        "Live Market",
        "Catalysts + Stream",
        "Signals + Reasoning",
        "Risk + Execution",
        "Trades + Journal",
        "Providers + Quality",
        "System Decisions",
        "Live Readiness",
        "Admin",
    ]
)

with tabs[0]:
    st.subheader("Real Market Data")
    st.caption("Rows are persisted from collector calls. Empty tables mean no data has been collected yet.")
    market_cols = st.columns([1, 1])
    with market_cols[0]:
        st.markdown("**Latest clean candles**")
        _table(snapshot["clean_candles"], label="clean candle", height=420)
    with market_cols[1]:
        st.markdown("**Latest feature calculations**")
        feature_rows = []
        for row in snapshot["features"]:
            feature_rows.append(
                {
                    "symbol": row["symbol"],
                    "source_timestamp": row["source_timestamp"],
                    "price": row["price"],
                    "vwap": row["vwap"],
                    "atr": row["atr"],
                    "relative_volume": row["relative_volume"],
                    "volume_spike_score": row["volume_spike_score"],
                    "liquidity_score": row["liquidity_score"],
                    "spread_score": row["spread_score"],
                    "feature_version": row["feature_version"],
                }
            )
        _table(feature_rows, label="feature", height=420)

    st.markdown("**Scanner results**")
    _table(snapshot["scanner_results"], label="scanner result", height=360)

    st.markdown("**Daily feature calculations**")
    _table(snapshot["daily_features"], label="daily feature", height=260)

    st.markdown("**Market regime snapshots**")
    _table(snapshot["regime_snapshots"], label="market regime snapshot", height=260)

with tabs[1]:
    st.subheader("Catalyst And Stream Intelligence")
    catalyst_cols = st.columns(2)
    with catalyst_cols[0]:
        st.markdown("**Alpaca market-data stream events**")
        stream_rows = []
        for row in snapshot["stream_events"]:
            stream_rows.append(
                {
                    "symbol": row["symbol"],
                    "event_type": row["event_type"],
                    "processed": row["processed"],
                    "provider": row["provider"],
                    "source_timestamp": row["source_timestamp"],
                    "reason": row["reason"],
                }
            )
        _table(stream_rows, label="stream event", height=360)
    with catalyst_cols[1]:
        st.markdown("**Scheduler runs**")
        _table(snapshot["scheduler_runs"], label="scheduler run", height=360)

    news_cols = st.columns(2)
    with news_cols[0]:
        st.markdown("**Clean news**")
        news_rows = []
        for row in snapshot["clean_news"]:
            news_rows.append(
                {
                    "symbol": row["symbol"],
                    "headline": row["headline"],
                    "confidence": row["source_confidence_score"],
                    "duplicate": row["duplicate_headline"],
                    "rumor": row["rumor_flag"],
                    "reason": row["reason"],
                    "source_timestamp": row["source_timestamp"],
                }
            )
        _table(news_rows, label="clean news", height=420)
    with news_cols[1]:
        st.markdown("**SEC filings**")
        filing_rows = []
        for row in snapshot["filings"]:
            filing_rows.append(
                {
                    "symbol": row["symbol"],
                    "form_type": row["form_type"],
                    "accession_number": row["accession_number"],
                    "source_timestamp": row["source_timestamp"],
                    "provider": row["provider"],
                }
            )
        _table(filing_rows, label="filing", height=420)

    event_cols = st.columns(2)
    with event_cols[0]:
        st.markdown("**Normalized events**")
        _table(snapshot["events"], label="event", height=360)
    with event_cols[1]:
        st.markdown("**Catalysts**")
        _table(snapshot["catalysts"], label="catalyst", height=360)

with tabs[2]:
    st.subheader("Signals And Reasoning")
    signal_rows = []
    for row in snapshot["signals"]:
        signal_rows.append(
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "strategy": row["strategy_id"],
                "status": row["status"],
                "direction": row["direction"],
                "entry_zone": _compact_dict(row["entry_zone"]),
                "stop_loss": row["stop_loss"],
                "target_1": row["target_1"],
                "target_2": row["target_2"],
                "risk_reward": row["risk_reward"],
                "confidence": row["confidence_score"],
                "invalidation": row["invalidation"],
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
            }
        )
    _table(signal_rows, label="signal", height=360)

    st.markdown("**Trade thesis reasoning**")
    thesis_rows = []
    for row in snapshot["trade_theses"]:
        thesis_rows.append(
            {
                "symbol": row["symbol"],
                "strategy": row["strategy_id"],
                "confidence": row["confidence"],
                "setup_quality": row["setup_quality"],
                "catalyst_quality": row["catalyst_quality"],
                "reason_for_trade": row["reason_for_trade"],
                "invalidation_reason": row["invalidation_reason"],
                "risks": _compact_dict(row["risks"]),
                "prompt_version": row["prompt_version"],
                "created_at": row["created_at"],
            }
        )
    _table(thesis_rows, label="trade thesis", height=420)

with tabs[3]:
    st.subheader("Risk And Execution")
    st.caption("Paper submission requires `ENVIRONMENT_MODE=paper`; otherwise the order is blocked and logged.")
    signals = snapshot["signals"]
    signal_options = {
        f"{row['symbol']} | {row['strategy_id']} | {row['created_at']} | {row['id'][:8]}": row["id"]
        for row in signals
    }
    if signal_options:
        selected_label = st.selectbox("Signal to risk-check / paper-submit", list(signal_options.keys()))
        risk_cols = st.columns(4)
        account_equity = risk_cols[0].number_input("Account equity", value=100_000.0, min_value=1.0)
        open_positions = risk_cols[1].number_input("Open positions", value=0, min_value=0)
        trades_today = risk_cols[2].number_input("Trades today", value=0, min_value=0)
        strategy_trades_today = risk_cols[3].number_input("Strategy trades today", value=0, min_value=0)
        risk_cols_2 = st.columns(4)
        daily_loss_pct = risk_cols_2[0].number_input("Daily loss %", value=0.0, min_value=0.0)
        weekly_loss_pct = risk_cols_2[1].number_input("Weekly loss %", value=0.0, min_value=0.0)
        sector_exposure_pct = risk_cols_2[2].number_input("Sector exposure %", value=0.0, min_value=0.0)
        internal_quantity = risk_cols_2[3].number_input("Internal position qty", value=0.0)
        broker_quantity = st.number_input("Broker position qty", value=0.0)

        if st.button("Run Risk Check + Paper Submit", width="stretch", disabled=not can_trade):
            result = service.submit_signal_to_paper(
                signal_id=signal_options[selected_label],
                account_equity=account_equity,
                open_positions=int(open_positions),
                daily_loss_pct=daily_loss_pct,
                weekly_loss_pct=weekly_loss_pct,
                sector_exposure_pct=sector_exposure_pct,
                trades_today=int(trades_today),
                strategy_trades_today=int(strategy_trades_today),
                internal_quantity=internal_quantity,
                broker_quantity=broker_quantity,
            )
            st.session_state["last_paper_submit"] = result
            st.rerun()
    else:
        st.info("No signals available yet. Collect market data and run a scan cycle first.")

    if "last_paper_submit" in st.session_state:
        with st.expander("Last paper submission decision", expanded=True):
            st.json(st.session_state["last_paper_submit"])

    exec_cols = st.columns(4)
    with exec_cols[0]:
        st.markdown("**Risk checks**")
        _table(snapshot["risk_checks"], label="risk check", height=360)
    with exec_cols[1]:
        st.markdown("**Orders**")
        _table(snapshot["orders"], label="order", height=360)
    with exec_cols[2]:
        st.markdown("**Fills**")
        _table(snapshot["fills"], label="fill", height=360)
    with exec_cols[3]:
        st.markdown("**Positions**")
        _table(snapshot["positions"], label="position", height=360)

    broker_cols = st.columns(3)
    with broker_cols[0]:
        st.markdown("**Broker sync logs**")
        _table(snapshot["broker_sync_logs"], label="broker sync log", height=300)
    with broker_cols[1]:
        st.markdown("**Broker account snapshots**")
        _table(snapshot["broker_account_snapshots"], label="broker account snapshot", height=300)
    with broker_cols[2]:
        st.markdown("**Execution errors**")
        _table(snapshot["execution_errors"], label="execution error", height=300)

    st.markdown("**Exposure snapshots**")
    _table(snapshot["exposure_snapshots"], label="exposure snapshot", height=300)

with tabs[4]:
    st.subheader("Trades And Journal")
    with st.form("journal_form", clear_on_submit=True):
        cols = st.columns(4)
        journal_symbol = cols[0].text_input("Symbol")
        journal_strategy = cols[1].text_input("Strategy ID", "VWAP_RECLAIM")
        actual_entry = cols[2].number_input("Actual entry", value=0.0, min_value=0.0)
        actual_exit = cols[3].number_input("Actual exit", value=0.0, min_value=0.0)
        pnl = st.number_input("PnL", value=0.0)
        entry_thesis = st.text_area("Entry thesis / reasoning")
        human_notes = st.text_area("Human notes")
        mistake_tags_raw = st.text_input("Mistake tags comma-separated")
        submitted = st.form_submit_button("Record Journal Entry", disabled=not can_trade)
        if submitted:
            if not journal_symbol.strip() or not entry_thesis.strip():
                st.error("Journal symbol and entry thesis are required.")
            else:
                service.repository.store_journal_entry(
                    symbol=journal_symbol,
                    strategy_id=journal_strategy or None,
                    entry_thesis=entry_thesis,
                    actual_entry=actual_entry or None,
                    actual_exit=actual_exit or None,
                    pnl=pnl,
                    human_notes=human_notes or None,
                    mistake_tags=[item.strip() for item in mistake_tags_raw.split(",") if item.strip()],
                    change_reason="Manual dashboard journal entry.",
                )
                st.success("Journal entry recorded.")
                st.rerun()

    _table(snapshot["journal"], label="journal", height=420)

    st.markdown("**Trade reviews**")
    _table(snapshot["ai_reviews"], label="trade review", height=320)

    review_cols = st.columns(2)
    with review_cols[0]:
        st.markdown("**Weekly reviews**")
        _table(snapshot["weekly_reviews"], label="weekly review", height=300)
    with review_cols[1]:
        st.markdown("**Learning recommendations**")
        _table(snapshot["strategy_recommendations"], label="strategy recommendation", height=300)

with tabs[5]:
    st.subheader("Providers, API Calls, And Data Quality")
    quality_cols = st.columns(4)
    with quality_cols[0]:
        st.markdown("**Provider capabilities**")
        _table(snapshot["providers"], label="provider capability", height=360)
    with quality_cols[1]:
        st.markdown("**Provider rate limits**")
        _table(snapshot["provider_rate_limits"], label="provider rate limit", height=360)
    with quality_cols[2]:
        st.markdown("**API call logs**")
        _table(snapshot["api_calls"], label="API call", height=360)
    with quality_cols[3]:
        st.markdown("**Data quality errors**")
        _table(snapshot["data_quality_errors"], label="data quality error", height=360)

    st.markdown("**Strategy registry**")
    _table(snapshot["strategies"], label="strategy", height=260)

    st.markdown("**Provider health**")
    _table(snapshot["provider_health"], label="provider health", height=300)

    st.markdown("**Worker health**")
    _table(snapshot["worker_heartbeats"], label="worker heartbeat", height=260)

    st.markdown("**Missing candle gaps**")
    _table(snapshot["missing_candle_gaps"], label="missing candle gap", height=260)

    st.markdown("**Backtest reports**")
    _table(snapshot["backtest_reports"], label="backtest report", height=260)

with tabs[6]:
    st.subheader("Decision Logs")
    st.caption("Scanner, signal, risk, execution, journal, and AI reasoning decisions are recorded here.")
    _table(snapshot["decisions"], label="decision log", height=520)

    st.markdown("**Audit logs**")
    _table(snapshot["audit_logs"], label="audit log", height=360)

with tabs[7]:
    st.subheader("Live Readiness")
    st.caption("Live execution is disabled by default and requires every readiness, approval, and gate check.")
    if can_admin:
        approval_cols = st.columns(2)
        with approval_cols[0]:
            with st.form("live_approval_create_form", clear_on_submit=True):
                st.markdown("**Create approval window**")
                approval_reason = st.text_area("Approval reason")
                approval_minutes = st.number_input(
                    "Approval minutes",
                    min_value=1,
                    max_value=max(1, int(settings.live_approval_max_age_minutes)),
                    value=min(30, max(1, int(settings.live_approval_max_age_minutes))),
                    step=1,
                )
                submitted = st.form_submit_button(
                    "Create Live Approval Window",
                    disabled=not approval_reason.strip(),
                )
                if submitted:
                    expires_at = datetime.now(UTC) + timedelta(minutes=int(approval_minutes))
                    row = service.repository.store_live_trading_approval(
                        approved_by=principal.username,
                        reason=approval_reason,
                        expires_at=expires_at,
                    )
                    service.repository.store_audit_log(
                        actor=principal.username,
                        event_type="LIVE_APPROVAL_CREATED",
                        entity_type="live_trading_approval",
                        entity_id=row.id,
                        reason=approval_reason,
                        payload={"expires_at": expires_at.isoformat(), "source": "dashboard"},
                    )
                    st.session_state["last_live_approval_action"] = {
                        "action": "created",
                        "approval_id": row.id,
                        "expires_at": expires_at.isoformat(),
                    }
                    st.rerun()
        with approval_cols[1]:
            active_approvals = [
                row
                for row in snapshot["live_trading_approvals"]
                if row.get("status") == "ACTIVE" and not row.get("revoked_at")
            ]
            with st.form("live_approval_revoke_form", clear_on_submit=True):
                st.markdown("**Revoke approval**")
                approval_ids = [row["id"] for row in active_approvals]
                approval_id = st.selectbox("Active approval", approval_ids, disabled=not approval_ids)
                revoke_reason = st.text_area("Revocation reason")
                submitted = st.form_submit_button(
                    "Revoke Live Approval",
                    disabled=not approval_ids or not revoke_reason.strip(),
                )
                if submitted:
                    row = service.repository.revoke_live_trading_approval(
                        approval_id=approval_id,
                        revoked_by=principal.username,
                        reason=revoke_reason,
                    )
                    st.session_state["last_live_approval_action"] = {
                        "action": "revoked",
                        "approval_id": row.id,
                        "reason": revoke_reason,
                    }
                    st.rerun()
    elif principal.role == AdminRole.TRADER.value:
        st.info("Admin role is required to create or revoke live approval windows.")

    reports = snapshot["live_readiness_reports"]
    _table(reports, label="live-readiness report", height=360)
    if reports:
        latest = reports[0]
        st.markdown("**Latest readiness checks**")
        checks = latest.get("checks") or []
        _table(checks, label="live-readiness check", height=460)

    st.markdown("**Live approvals**")
    _table(snapshot["live_trading_approvals"], label="live approval", height=260)

    st.markdown("**Kill switches**")
    _table(snapshot["kill_switches"], label="kill switch", height=260)

    st.markdown("**Strategy approval requests**")
    _table(snapshot["strategy_approval_requests"], label="strategy approval request", height=300)

with tabs[8]:
    st.subheader("Admin Users")
    if not can_admin:
        st.info("Admin role is required to manage users.")
    else:
        admin_users = service.repository.list_admin_users(100)
        _table(admin_users, label="admin user", height=300)
        admin_usernames = [row["username"] for row in admin_users]
        role_options = [role.value for role in AdminRole]

        create_cols = st.columns(2)
        with create_cols[0]:
            with st.form("admin_user_upsert_form", clear_on_submit=True):
                st.markdown("**Create or update user**")
                admin_username = st.text_input("Username")
                admin_password = st.text_input("Password", type="password")
                admin_role = st.selectbox("Role", role_options, index=role_options.index(AdminRole.VIEWER.value))
                admin_reason = st.text_area("Reason")
                submitted = st.form_submit_button(
                    "Save User",
                    disabled=not admin_username.strip() or len(admin_password) < 12 or not admin_reason.strip(),
                )
                if submitted:
                    if admin_username.strip() == principal.username and admin_role != AdminRole.ADMIN.value:
                        st.error("Admins cannot demote their own active session user.")
                    else:
                        row = service.repository.upsert_admin_user(
                            username=admin_username.strip(),
                            password_hash=hash_password(admin_password),
                            role=admin_role,
                            reason=admin_reason,
                        )
                        revoked_sessions = service.repository.revoke_admin_sessions_for_user(
                            user_id=row.id,
                            reason="Admin user password or role updated from dashboard; active sessions revoked.",
                        )
                        service.repository.store_audit_log(
                            actor=principal.username,
                            event_type="ADMIN_USER_UPSERTED",
                            entity_type="admin_user",
                            entity_id=row.id,
                            reason=admin_reason,
                            payload={
                                "username": row.username,
                                "role": row.role,
                                "is_active": row.is_active,
                                "revoked_sessions": revoked_sessions,
                                "source": "dashboard",
                            },
                        )
                        st.session_state["last_admin_user_action"] = {
                            "action": "saved",
                            "username": row.username,
                            "role": row.role,
                        }
                        st.rerun()

        with create_cols[1]:
            with st.form("admin_user_role_form", clear_on_submit=True):
                st.markdown("**Change role**")
                role_username = st.selectbox("User", admin_usernames, disabled=not admin_usernames)
                role_value = st.selectbox("New role", role_options, key="admin_user_role_select")
                role_reason = st.text_area("Reason", key="admin_user_role_reason")
                submitted = st.form_submit_button(
                    "Change Role",
                    disabled=not admin_usernames or not role_reason.strip(),
                )
                if submitted:
                    if role_username == principal.username and role_value != AdminRole.ADMIN.value:
                        st.error("Admins cannot demote their own active session user.")
                    else:
                        row = service.repository.set_admin_user_role(
                            username=role_username,
                            role=role_value,
                            reason=role_reason,
                        )
                        service.repository.store_audit_log(
                            actor=principal.username,
                            event_type="ADMIN_USER_ROLE_CHANGED",
                            entity_type="admin_user",
                            entity_id=row.id,
                            reason=role_reason,
                            payload={"username": row.username, "role": row.role, "source": "dashboard"},
                        )
                        st.session_state["last_admin_user_action"] = {
                            "action": "role_changed",
                            "username": row.username,
                            "role": row.role,
                        }
                        st.rerun()

        state_cols = st.columns(2)
        with state_cols[0]:
            with st.form("admin_user_active_form", clear_on_submit=True):
                st.markdown("**Activate or deactivate**")
                active_username = st.selectbox("User", admin_usernames, key="admin_user_active", disabled=not admin_usernames)
                is_active = st.checkbox("Active", value=True)
                active_reason = st.text_area("Reason", key="admin_user_active_reason")
                submitted = st.form_submit_button(
                    "Update Active State",
                    disabled=not admin_usernames or not active_reason.strip(),
                )
                if submitted:
                    if active_username == principal.username and not is_active:
                        st.error("Admins cannot deactivate their own active session user.")
                    else:
                        row = service.repository.set_admin_user_active(
                            username=active_username,
                            is_active=is_active,
                            reason=active_reason,
                        )
                        revoked_sessions = 0
                        if not row.is_active:
                            revoked_sessions = service.repository.revoke_admin_sessions_for_user(
                                user_id=row.id,
                                reason="Admin user deactivated from dashboard; active sessions revoked.",
                            )
                        service.repository.store_audit_log(
                            actor=principal.username,
                            event_type="ADMIN_USER_ACTIVE_CHANGED",
                            entity_type="admin_user",
                            entity_id=row.id,
                            reason=active_reason,
                            payload={
                                "username": row.username,
                                "is_active": row.is_active,
                                "revoked_sessions": revoked_sessions,
                                "source": "dashboard",
                            },
                        )
                        st.session_state["last_admin_user_action"] = {
                            "action": "active_state_changed",
                            "username": row.username,
                            "is_active": row.is_active,
                        }
                        st.rerun()

        with state_cols[1]:
            with st.form("admin_user_unlock_form", clear_on_submit=True):
                st.markdown("**Unlock user**")
                unlock_username = st.selectbox("User", admin_usernames, key="admin_user_unlock", disabled=not admin_usernames)
                unlock_reason = st.text_area("Reason", key="admin_user_unlock_reason")
                submitted = st.form_submit_button(
                    "Clear Lockout",
                    disabled=not admin_usernames or not unlock_reason.strip(),
                )
                if submitted:
                    row = service.repository.clear_admin_user_lockout(
                        username=unlock_username,
                        reason=unlock_reason,
                    )
                    service.repository.store_audit_log(
                        actor=principal.username,
                        event_type="ADMIN_USER_UNLOCKED",
                        entity_type="admin_user",
                        entity_id=row.id,
                        reason=unlock_reason,
                        payload={"username": row.username, "source": "dashboard"},
                    )
                    st.session_state["last_admin_user_action"] = {
                        "action": "unlocked",
                        "username": row.username,
                    }
                    st.rerun()

if auto_refresh:
    time.sleep(max(5, int(settings.dashboard_refresh_seconds)))
    st.rerun()
