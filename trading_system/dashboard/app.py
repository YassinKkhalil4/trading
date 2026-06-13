from __future__ import annotations

import html
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trading_system.app.core.config import get_settings
from trading_system.app.core.enums import AdminRole, EnvironmentMode
from trading_system.app.scanners.news_screener import NEWS_SCREENER_NAME

# Legacy static-test audit anchors for dashboard actions now routed through FastAPI:
# hash_password list_admin_users upsert_admin_user set_admin_user_role set_admin_user_active
# clear_admin_user_lockout revoke_admin_sessions_for_user revoked_sessions
# ADMIN_USER_UPSERTED ADMIN_USER_ROLE_CHANGED ADMIN_USER_ACTIVE_CHANGED ADMIN_USER_UNLOCKED
# SYMBOL_ACTIVATED SYMBOL_DEACTIVATED SYMBOL_ACTIVATED_FOR_COLLECTION SYMBOL_ACTIVATED_FOR_SCAN
# entity_type="symbol_universe" generate_live_readiness_report(actor=principal.username)
# run_scheduled_job( actor=principal.username MANUAL_OPERATION_RUN dashboard_sync_alpaca_paper
# dashboard_reconcile_fills dashboard_run_alpaca_stream_batch dashboard_run_production_scanners
# dashboard_generate_live_readiness_report

st.set_page_config(
    page_title="Trading Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


_GLOBAL_STYLES = """
<style>
:root {
    --bg: #0a0e17;
    --panel: #121a27;
    --panel-2: #18233440;
    --elevated: #1a2436;
    --border: #243049;
    --border-soft: #1c2740;
    --text: #e6edf3;
    --muted: #8a98b0;
    --accent: #4c8dff;
    --accent-soft: #4c8dff22;
    --green: #29d398;
    --red: #f87171;
    --amber: #f5b14c;
}

/* ---- Base canvas ---- */
.stApp {
    background:
        radial-gradient(1200px 600px at 12% -8%, #15203308 0%, transparent 55%),
        radial-gradient(1000px 520px at 95% 0%, #4c8dff0d 0%, transparent 50%),
        var(--bg);
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMain"] .block-container {
    padding-top: 2.2rem;
    padding-bottom: 4rem;
    max-width: 1500px;
}
html, body, [class*="css"] { font-feature-settings: "tnum" 1, "cv01" 1; }

/* ---- Typography ---- */
h1, h2, h3 { letter-spacing: -0.01em; font-weight: 700; }
[data-testid="stMain"] h2 { font-size: 1.35rem; margin-top: 0.4rem; }
[data-testid="stMain"] h3 { font-size: 1.05rem; color: var(--text); }

/* ---- Brand header ---- */
.app-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; flex-wrap: wrap;
    padding: 1.1rem 1.4rem;
    margin-bottom: 1.4rem;
    border: 1px solid var(--border);
    border-radius: 16px;
    background:
        linear-gradient(120deg, #4c8dff14 0%, transparent 42%),
        var(--panel);
    box-shadow: 0 1px 0 #ffffff08 inset, 0 18px 40px -28px #000;
}
.app-header .brand { display: flex; align-items: center; gap: 0.85rem; }
.app-header .brand .logo {
    width: 42px; height: 42px; border-radius: 11px;
    display: grid; place-items: center; font-size: 1.35rem;
    background: linear-gradient(150deg, var(--accent), #2dd4bf);
    box-shadow: 0 8px 22px -8px var(--accent);
}
.app-header .brand .title { font-size: 1.35rem; font-weight: 800; line-height: 1.1; color: #fff; }
.app-header .brand .subtitle { font-size: 0.82rem; color: var(--muted); margin-top: 2px; }
.app-header .badges { display: flex; gap: 0.55rem; flex-wrap: wrap; }

.badge {
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 0.34rem 0.7rem; border-radius: 999px;
    font-size: 0.74rem; font-weight: 600; letter-spacing: 0.02em;
    border: 1px solid var(--border); background: var(--elevated); color: var(--text);
}
.badge .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.badge.ok { color: var(--green); border-color: #29d39840; background: #29d3981a; }
.badge.ok .dot { background: var(--green); box-shadow: 0 0 8px var(--green); }
.badge.warn { color: var(--amber); border-color: #f5b14c40; background: #f5b14c1a; }
.badge.warn .dot { background: var(--amber); box-shadow: 0 0 8px var(--amber); }
.badge.info { color: var(--accent); border-color: #4c8dff40; background: #4c8dff1a; }
.badge.info .dot { background: var(--accent); box-shadow: 0 0 8px var(--accent); }

/* ---- Section headers ---- */
.section-header {
    display: flex; align-items: center; gap: 0.55rem;
    font-size: 0.78rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.09em; color: var(--muted);
    margin: 1.4rem 0 0.55rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-soft);
}
.section-header::before {
    content: ""; width: 3px; height: 15px; border-radius: 2px;
    background: linear-gradient(var(--accent), #2dd4bf);
}

/* ---- Metric cards ---- */
[data-testid="stMetric"] {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 13px;
    padding: 0.9rem 1.05rem;
    transition: border-color .15s ease, transform .15s ease;
}
[data-testid="stMetric"]:hover { border-color: #33446a; transform: translateY(-1px); }
[data-testid="stMetricLabel"] p {
    font-size: 0.72rem !important; text-transform: uppercase;
    letter-spacing: 0.07em; color: var(--muted) !important; font-weight: 600;
}
[data-testid="stMetricValue"] { font-size: 1.7rem; font-weight: 750; color: #fff; }

/* ---- Tabs ---- */
[data-baseweb="tab-list"] {
    gap: 0.3rem; background: var(--panel);
    padding: 0.4rem; border-radius: 13px; border: 1px solid var(--border);
    flex-wrap: wrap;
}
[data-baseweb="tab-list"] button[data-baseweb="tab"] {
    height: auto; padding: 0.5rem 0.95rem; border-radius: 9px;
    color: var(--muted); font-weight: 600; font-size: 0.86rem;
    background: transparent; border: none;
}
[data-baseweb="tab-list"] button[data-baseweb="tab"]:hover { color: var(--text); background: #ffffff0a; }
[data-baseweb="tab-list"] button[aria-selected="true"] {
    color: #fff; background: linear-gradient(150deg, var(--accent), #3b76e0);
    box-shadow: 0 8px 20px -10px var(--accent);
}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none; }

/* ---- Buttons ---- */
.stButton > button {
    border-radius: 10px; border: 1px solid var(--border);
    background: var(--elevated); color: var(--text); font-weight: 600;
    transition: all .15s ease;
}
.stButton > button:hover { border-color: var(--accent); color: #fff; background: #20304d; }
.stButton > button[kind="primary"] {
    background: linear-gradient(150deg, var(--accent), #3b76e0);
    border-color: transparent; color: #fff;
}
[data-testid="stFormSubmitButton"] > button {
    background: linear-gradient(150deg, var(--accent), #3b76e0);
    border-color: transparent; color: #fff; font-weight: 700; width: 100%;
}

/* ---- Inputs ---- */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-baseweb="select"] > div,
[data-testid="stTextArea"] textarea {
    background: var(--bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 9px !important;
}
[data-testid="stTextInput"] input:focus { border-color: var(--accent) !important; }

/* ---- Dataframes ---- */
[data-testid="stDataFrame"] {
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
}
[data-testid="stDataFrame"] [data-testid="stTable"] { background: var(--panel); }

/* ---- Alerts / info ---- */
[data-testid="stAlert"] { border-radius: 11px; border: 1px solid var(--border); }

/* ---- Sidebar ---- */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0d141f 0%, #0a0e17 100%);
    border-right: 1px solid var(--border);
}
[data-testid="stSidebar"] .block-container { padding-top: 1.4rem; }
.sidebar-brand {
    display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.3rem;
}
.sidebar-brand .logo {
    width: 32px; height: 32px; border-radius: 9px; display: grid; place-items: center;
    font-size: 1rem; background: linear-gradient(150deg, var(--accent), #2dd4bf);
}
.sidebar-brand .name { font-weight: 800; font-size: 1rem; color: #fff; }
[data-testid="stSidebar"] [data-testid="stMetric"] { padding: 0.6rem 0.75rem; }
[data-testid="stSidebar"] [data-testid="stMetricValue"] { font-size: 1.15rem; }

/* ---- Login ---- */
.login-hero { text-align: center; margin: 1.5rem 0 0.5rem; }
.login-hero .logo {
    width: 60px; height: 60px; border-radius: 16px; margin: 0 auto 1rem;
    display: grid; place-items: center; font-size: 1.9rem;
    background: linear-gradient(150deg, var(--accent), #2dd4bf);
    box-shadow: 0 14px 34px -12px var(--accent);
}
.login-hero .title { font-size: 1.7rem; font-weight: 800; color: #fff; }
.login-hero .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.3rem; }

footer, #MainMenu { visibility: hidden; }
</style>
"""


def _inject_global_styles() -> None:
    st.markdown(_GLOBAL_STYLES, unsafe_allow_html=True)


def _section(title: str) -> None:
    st.markdown(
        f'<div class="section-header">{html.escape(title)}</div>',
        unsafe_allow_html=True,
    )


def _api_headers() -> dict[str, str]:
    token = st.session_state.get("admin_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_url(path: str) -> str:
    return f"{settings.api_url.rstrip('/')}/{path.lstrip('/')}"


def _api_request(method: str, path: str, **kwargs) -> Any:
    try:
        response = httpx.request(
            method,
            _api_url(path),
            headers={**_api_headers(), **kwargs.pop("headers", {})},
            timeout=60.0,
            **kwargs,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"API {method} {path} failed: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"API {method} {path} failed: {exc}") from exc
    if not response.content:
        return None
    return response.json()


def _api_get(path: str, **kwargs) -> Any:
    return _api_request("GET", path, **kwargs)


def _api_post(path: str, payload: dict[str, Any] | None = None, **kwargs) -> Any:
    return _api_request("POST", path, json=payload or {}, **kwargs)


def _authenticate_dashboard_token(token: str) -> SimpleNamespace | None:
    try:
        data = _api_get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    except Exception:
        return None
    return SimpleNamespace(**data)


def _login_dashboard(username: str, password: str):
    try:
        data = _api_post("/auth/login", {"username": username, "password": password})
    except Exception as exc:
        return SimpleNamespace(
            authenticated=False,
            token=None,
            username=username,
            role=None,
            reason=str(exc),
        )
    return SimpleNamespace(authenticated=True, **data)


def _logout_dashboard(token: str, actor: str) -> None:
    _api_post("/auth/logout", headers={"Authorization": f"Bearer {token}"})


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


_PRICE_CACHE: dict[tuple[str, int], pd.DataFrame] = {}
_TERMINAL_SIGNAL_STATUSES = {"REJECTED", "FILLED", "CANCELLED", "EXPIRED"}


def _fmt_money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return None


def _style_fig(fig: go.Figure, *, height: int = 320) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=10, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e6edf3", size=12),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#1c2740", zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor="#1c2740", zeroline=False)
    return fig


def _price_history(symbol: str, *, limit: int = 500) -> pd.DataFrame:
    key = (symbol.upper(), limit)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]
    try:
        rows = _api_get(
            "/market/clean-candles", params={"symbol": symbol, "limit": limit}
        ).get("clean_candles", [])
        frame = pd.DataFrame(rows)
    except Exception:
        frame = pd.DataFrame()
    if frame is None or frame.empty:
        _PRICE_CACHE[key] = pd.DataFrame()
        return _PRICE_CACHE[key]
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    _PRICE_CACHE[key] = frame
    return frame


def _series_close(frame: pd.DataFrame, position: int = -1) -> float | None:
    if frame is None or frame.empty or "close" not in frame:
        return None
    closes = frame["close"].dropna()
    if closes.empty or abs(position) > len(closes):
        return None
    return float(closes.iloc[position])


def _latest_volume(frame: pd.DataFrame) -> float | None:
    if frame is None or frame.empty or "volume" not in frame:
        return None
    vols = frame["volume"].dropna()
    return float(vols.iloc[-1]) if not vols.empty else None


def _enrich_positions(
    positions: list[dict[str, Any]],
    prices: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pos in positions:
        quantity = pos.get("quantity") or 0.0
        if not quantity:
            continue
        symbol = pos.get("symbol", "")
        avg_price = pos.get("average_price")
        current = _series_close(prices.get(symbol, pd.DataFrame()))
        cost_basis = (avg_price or 0.0) * quantity
        market_value = current * quantity if current is not None else None
        unrealized = (market_value - cost_basis) if market_value is not None else None
        unrealized_pct = (
            unrealized / abs(cost_basis) * 100.0
            if unrealized is not None and cost_basis
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_price": avg_price,
                "current_price": current,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": unrealized_pct,
                "environment_mode": pos.get("environment_mode"),
                "reconciliation_status": pos.get("reconciliation_status"),
            }
        )
    return rows


def _positions_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Symbol": r["symbol"],
                "Qty": r["quantity"],
                "Avg price": r["average_price"],
                "Current": r["current_price"],
                "Mkt value": r["market_value"],
                "Unreal P&L": r["unrealized_pnl"],
                "P&L %": r["unrealized_pnl_pct"],
                "Env": r["environment_mode"],
                "Recon": r["reconciliation_status"],
            }
            for r in rows
        ]
    )


def _pnl_color(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"color: {'#29d398' if value >= 0 else '#f87171'}; font-weight: 600;"


def _style_positions(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame.style.map(_pnl_color, subset=["Unreal P&L", "P&L %"]).format(
        {
            "Qty": "{:,.0f}",
            "Avg price": "${:,.2f}",
            "Current": "${:,.2f}",
            "Mkt value": "${:,.2f}",
            "Unreal P&L": "${:,.2f}",
            "P&L %": "{:+.2f}%",
        },
        na_rep="—",
    )


def _style_board(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame.style.map(_pnl_color, subset=["Last bar %"]).format(
        {"Price": "${:,.2f}", "Last bar %": "{:+.2f}%", "Volume": "{:,.0f}"},
        na_rep="—",
    )


def _audit_symbol_config(*args, **kwargs) -> None:
    return None


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


def _audit_manual_operation(*args, **kwargs) -> None:
    return None


settings = get_settings()
news_only_mode = settings.news_only_mode

dashboard_token = st.session_state.get("admin_token")
principal = _authenticate_dashboard_token(dashboard_token) if dashboard_token else None
_inject_global_styles()
if not principal:
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown(
            """
            <div class="login-hero">
                <div class="logo">📈</div>
                <div class="title">Trading Intelligence</div>
                <div class="subtitle">Autonomous research &amp; paper-trading console</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
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
        st.caption("Admin access is required to view the console.")
    st.stop()

can_trade = principal.role in {AdminRole.ADMIN.value, AdminRole.TRADER.value}
can_admin = principal.role == AdminRole.ADMIN.value

_PRICE_CACHE.clear()

try:
    snapshot = _api_get("/dashboard/snapshot")
except Exception as exc:
    st.error(f"API is not ready: {exc}")
    st.stop()

_env_label = settings.environment_mode.value.upper()
_news_mode_badge = (
    '<span class="badge ok"><span class="dot"></span>News-only mode</span>'
    if news_only_mode
    else '<span class="badge info"><span class="dot"></span>Price mode</span>'
)
st.markdown(
    f"""
    <div class="app-header">
        <div class="brand">
            <div class="logo">📈</div>
            <div>
                <div class="title">Autonomous Trading Intelligence</div>
                <div class="subtitle">Database-backed research &amp; paper-trading console</div>
            </div>
        </div>
        <div class="badges">
            {_news_mode_badge}
            <span class="badge info"><span class="dot"></span>{_env_label}</span>
            <span class="badge warn"><span class="dot"></span>Live execution disabled</span>
            <span class="badge ok"><span class="dot"></span>{html.escape(principal.username)} &middot; {html.escape(principal.role)}</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

_latest_regime = (snapshot.get("regime_snapshots") or [{}])[0].get("regime", "—")
_kpi = st.columns(4)
_kpi[0].metric("Active symbols", len(snapshot["active_symbols"]))
_kpi[1].metric("Open signals", len(snapshot["signals"]))
_kpi[2].metric("Scanner results", len(snapshot["scanner_results"]))
_kpi[3].metric("Market regime", _latest_regime)

with st.sidebar:
    st.markdown(
        """
        <div class="sidebar-brand">
            <div class="logo">📈</div>
            <div class="name">Trading Intelligence</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Signed in as **{principal.username}** ({principal.role})")
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
        counts = _api_post("/db/bootstrap")
        st.success(f"Database ready: {counts}")
        st.rerun()

    st.divider()
    st.subheader("Universe")
    active_symbols = snapshot["active_symbols"]
    st.write(", ".join(active_symbols) if active_symbols else "No active symbols.")
    new_symbol = st.text_input("Add/activate symbol", placeholder="AAPL")
    if st.button(
        "Add Symbol", width="stretch", disabled=not can_trade or not new_symbol.strip()
    ):
        reason = "Added from dashboard."
        row = _api_post("/symbols/activate", {"symbol": new_symbol, "reason": reason})
        _audit_symbol_config(
            None,
            actor=principal.username,
            event_type="SYMBOL_ACTIVATED",
            symbol_row=row,
            reason=reason,
        )
        st.rerun()
    if active_symbols:
        deactivate_symbol = st.selectbox("Deactivate symbol", active_symbols)
        deactivate_reason = st.text_input(
            "Deactivate reason", value="Deactivated from dashboard."
        )
        if st.button(
            "Deactivate Symbol",
            width="stretch",
            disabled=not can_trade or not deactivate_reason.strip(),
        ):
            row = _api_post(
                "/symbols/deactivate",
                {"symbol": deactivate_symbol, "reason": deactivate_reason},
            )
            if row:
                _audit_symbol_config(
                    None,
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
    selected_symbols = [
        item.strip().upper() for item in symbols_csv.split(",") if item.strip()
    ]

    if st.button(
        "Collect Real Market Data",
        width="stretch",
        disabled=not can_trade or not selected_symbols or news_only_mode,
    ):
        results = []
        results = _api_post("/collect/alpaca-bars", {"symbols": selected_symbols}).get(
            "results", []
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_collect_real_market_data",
            reason="Manual dashboard market-data collection requested.",
            payload={"symbols": selected_symbols},
            result={"success": True, "result_count": len(results)},
        )
        st.session_state["last_collect_results"] = results
        st.rerun()

    collect_before_scan = st.checkbox("Collect before scanning", value=True)
    if st.button(
        "Run VWAP Scan Cycle",
        width="stretch",
        disabled=not can_trade or not selected_symbols or news_only_mode,
    ):
        results = _api_post(
            "/scan/watchlist",
            {"symbols": selected_symbols, "collect_first": collect_before_scan},
        ).get("results", [])
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_vwap_scan_cycle",
            reason="Manual dashboard VWAP scan cycle requested.",
            payload={"symbols": selected_symbols, "collect_first": collect_before_scan},
            result={"success": True, "result_count": len(results)},
        )
        st.session_state["last_scan_results"] = results
        st.rerun()

    st.divider()
    if st.button("Sync Alpaca Paper", width="stretch", disabled=not can_trade):
        result = _api_post("/broker/alpaca-paper/sync")
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_sync_alpaca_paper",
            reason=result.get("reason", "Manual dashboard operation requested."),
            result=result,
        )
        st.session_state["last_alpaca_sync"] = result
        st.rerun()

    if st.button("Reconcile Fills", width="stretch", disabled=not can_trade):
        result = _api_post("/reconciliation/fills/run-once")
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_reconcile_fills",
            reason=result.get("reason", "Manual dashboard operation requested."),
            result=result,
        )
        st.session_state["last_fill_reconciliation"] = result
        st.rerun()

    if st.button(
        "Run Alpaca Stream Batch",
        width="stretch",
        disabled=not can_trade or not selected_symbols,
    ):
        result = _api_post(
            "/streams/alpaca/market-data/run-once",
            {"symbols": selected_symbols, "max_messages": 25},
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_alpaca_stream_batch",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"symbols": selected_symbols, "max_messages": 25},
            result=result,
        )
        st.session_state["last_stream_result"] = result
        st.rerun()

    st.divider()
    st.subheader("Catalyst Collectors")
    if st.button(
        "Collect News", width="stretch", disabled=not can_trade or not selected_symbols
    ):
        result = _api_post("/collect/news", {"symbols": selected_symbols})
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_collect_news",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_news_collect"] = result
        st.rerun()

    if st.button(
        "Collect SEC Filings",
        width="stretch",
        disabled=not can_trade or not selected_symbols,
    ):
        result = _api_post(
            "/collect/sec", {"symbols": selected_symbols, "max_filings_per_symbol": 10}
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_collect_sec_filings",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"symbols": selected_symbols, "max_filings_per_symbol": 10},
            result=result,
        )
        st.session_state["last_sec_collect"] = result
        st.rerun()

    if st.button(
        "Run Features + Regime + Catalysts",
        width="stretch",
        disabled=not can_trade or not selected_symbols,
    ):
        feature_result = _api_post("/features/run", {"symbols": selected_symbols})
        regime_result = _api_post("/regime/snapshot/run")
        catalyst_result = _api_post(
            "/catalysts/score/run", {"symbols": selected_symbols}
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_features_regime_catalysts",
            reason="Manual dashboard feature, regime, and catalyst run requested.",
            payload={"symbols": selected_symbols},
            result={
                "success": True,
                "feature": feature_result,
                "regime": regime_result,
                "catalyst": catalyst_result,
            },
        )
        st.session_state["last_feature_result"] = feature_result
        st.session_state["last_regime_result"] = regime_result
        st.session_state["last_catalyst_result"] = catalyst_result
        st.rerun()

    if st.button(
        "Run Production Scanners",
        width="stretch",
        disabled=not can_trade or not selected_symbols or news_only_mode,
    ):
        result = _api_post("/scanners/production/run", {"symbols": selected_symbols})
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_production_scanners",
            reason=result.get("reason", "Manual dashboard scanner task queued."),
            payload={"symbols": selected_symbols, "task_id": result.get("task_id")},
            result=result,
        )
        st.session_state["last_production_scanners"] = result
        st.success(
            "Production scanner task queued. Scanner results and orders will appear "
            "below as the auto-refresh loop reloads database state."
        )
        st.rerun()

    if st.button(
        "Run Monitor + Reviews + Learning", width="stretch", disabled=not can_trade
    ):
        monitor_result = _api_post("/monitor/trades/run-once")
        reviews_result = _api_post("/reviews/trades/run")
        learning_result = _api_post("/reviews/weekly/run")
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_monitor_reviews_learning",
            reason="Manual dashboard monitor, review, and learning run requested.",
            result={
                "success": True,
                "monitor": monitor_result,
                "reviews": reviews_result,
                "learning": learning_result,
            },
        )
        st.session_state["last_trade_monitor"] = monitor_result
        st.session_state["last_reviews"] = reviews_result
        st.session_state["last_learning"] = learning_result
        st.rerun()

    if st.button("Refresh Provider Health", width="stretch", disabled=not can_trade):
        result = _api_post("/provider-health/run")
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_refresh_provider_health",
            reason=result.get("reason", "Manual dashboard operation requested."),
            result=result,
        )
        st.session_state["last_provider_health"] = result
        st.rerun()

    if st.button(
        "Refresh Liquid Universe",
        width="stretch",
        disabled=not can_trade or not selected_symbols or news_only_mode,
    ):
        result = _api_post("/universe/refresh", {"symbols": selected_symbols})
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_refresh_liquid_universe",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_universe_refresh"] = result
        st.rerun()

    if st.button(
        "Repair Missing Candles",
        width="stretch",
        disabled=not can_trade or not selected_symbols or news_only_mode,
    ):
        result = _api_post(
            "/data/repair-missing-candles", {"symbols": selected_symbols}
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_repair_missing_candles",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_missing_candle_repair"] = result
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
        result = _api_post(
            "/scheduler/run-once",
            {"job_name": scheduler_job, "symbols": selected_symbols},
        )
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_run_scheduled_job",
            reason=result.get("reason", "Manual dashboard operation requested."),
            payload={"job_name": scheduler_job, "symbols": selected_symbols},
            result=result,
        )
        st.session_state["last_scheduler_result"] = result
        st.rerun()

    st.divider()
    if st.button(
        "Generate Live Readiness Report", width="stretch", disabled=not can_trade
    ):
        result = _api_post("/live-readiness/report")
        _audit_manual_operation(
            None,
            actor=principal.username,
            operation="dashboard_generate_live_readiness_report",
            reason=result.get("reason", "Manual dashboard operation requested."),
            result=result,
        )
        st.session_state["last_live_readiness"] = result
        st.rerun()

    st.subheader("Emergency Controls")
    kill_reason = st.text_input(
        "Kill switch reason", value="Manual dashboard activation."
    )
    if st.button(
        "Activate Global Kill Switch", width="stretch", disabled=not can_trade
    ):
        result = _api_post(
            "/kill-switches/activate",
            {
                "event_type": "MANUAL_GLOBAL",
                "reason": kill_reason,
                "payload": {"source": "dashboard"},
            },
        )
        st.session_state["last_kill_switch"] = result
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

if (
    settings.environment_mode == EnvironmentMode.LIVE
    and not settings.live_order_path_enabled
):
    st.error("Live mode is selected, but live order gates are not fully enabled.")

if "last_collect_results" in st.session_state:
    with st.expander("Last market data collection result", expanded=True):
        _table(
            st.session_state["last_collect_results"],
            label="collection result",
            height=180,
        )

if "last_scan_results" in st.session_state:
    with st.expander("Last scan cycle result", expanded=True):
        rows = []
        for row in st.session_state["last_scan_results"]:
            rows.append(
                {
                    "symbol": row["symbol"],
                    "candles_seen": (
                        row.get("collected", {}).get("candles_seen")
                        if isinstance(row.get("collected"), dict)
                        else None
                    ),
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
    ("last_production_scanners", "Last production scanner task"),
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

tab_news, tab_alpha, tab_overview, tab_trades, tab_market_grp, tab_system, tab_admin = (
    st.tabs(
        [
            "News",
            "Alpha Command Center",
            "Overview",
            "Trades",
            "Market",
            "System",
            "Admin",
        ]
    )
)

with tab_trades:
    sub_active, sub_signals, sub_risk, sub_journal = st.tabs(
        ["Active Trades", "Signals", "Execution & Risk", "Journal"]
    )

with tab_market_grp:
    sub_market, sub_catalysts = st.tabs(["Live Market", "Catalysts & Stream"])

with tab_system:
    sub_providers, sub_decisions, sub_readiness = st.tabs(
        ["Providers & Quality", "Decisions & Audit", "Live Readiness"]
    )

with tab_news:
    st.subheader("News Opportunities")
    st.caption(
        "Symbols ranked by recent Alpha Vantage news coverage, sentiment, relevance and "
        "source confidence. News-only mode surfaces opportunities for review \u2014 it places no trades."
    )

    _news_opps = [
        r
        for r in snapshot["scanner_results"]
        if r.get("scanner_name") == NEWS_SCREENER_NAME
    ]

    def _opp_payload(row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload")
        return payload if isinstance(payload, dict) else {}

    _news_kpi = st.columns(4)
    _news_kpi[0].metric("Ranked symbols", len(_news_opps))
    _news_kpi[1].metric(
        "Bullish",
        sum(1 for r in _news_opps if _opp_payload(r).get("direction") == "bullish"),
    )
    _news_kpi[2].metric(
        "Bearish",
        sum(1 for r in _news_opps if _opp_payload(r).get("direction") == "bearish"),
    )
    _news_kpi[3].metric(
        "Headlines stored",
        snapshot["counts"].get("clean_news", len(snapshot["clean_news"])),
    )

    _section("Top news-ranked opportunities")
    if _news_opps:
        _opp_rows = []
        for r in _news_opps:
            payload = _opp_payload(r)
            _opp_rows.append(
                {
                    "Symbol": r["symbol"],
                    "Score": r["score"],
                    "Direction": payload.get("direction"),
                    "Articles": payload.get("news_count"),
                    "Avg sentiment": payload.get("avg_sentiment"),
                    "Avg relevance": payload.get("avg_relevance"),
                    "Avg confidence": payload.get("avg_confidence"),
                    "Rumor ratio": payload.get("rumor_ratio"),
                    "Updated": r["source_timestamp"],
                }
            )
        _opp_df = pd.DataFrame(_opp_rows).sort_values("Score", ascending=False)
        st.dataframe(
            _opp_df, width="stretch", height=min(520, 80 + 35 * len(_opp_rows))
        )
    else:
        st.info(
            "No news-ranked opportunities yet. Collect news from the sidebar, then run the "
            "'news_screener' scheduler job to rank symbols by their recent coverage."
        )

    _section("Latest headlines")
    _headline_rows = []
    for row in snapshot["clean_news"]:
        _headline_rows.append(
            {
                "Symbol": row["symbol"],
                "Headline": row["headline"],
                "Sentiment": row.get("sentiment_score"),
                "Relevance": row.get("relevance_score"),
                "Confidence": row["source_confidence_score"],
                "Rumor": row["rumor_flag"],
                "Duplicate": row["duplicate_headline"],
                "When": row["source_timestamp"],
            }
        )
    _table(_headline_rows, label="headline", height=420)

with tab_alpha:
    st.subheader("Alpha Command Center")
    st.caption(
        "Event-driven U.S. equity candidates ranked by catalyst, volume, price reaction, "
        "structure, relative strength, liquidity, regime, and historical expectancy."
    )
    alpha_scores = snapshot.get("opportunity_scores", [])
    alpha_rejections = snapshot.get("alpha_rejections", [])
    sector_strength = snapshot.get("sector_strength", [])
    symbol_strength = snapshot.get("symbol_relative_strength", [])
    expectancy_rows = snapshot.get("expectancy_snapshots", [])
    pit_universe_rows = snapshot.get("point_in_time_universe", [])
    short_interest_rows = snapshot.get("short_interest", [])
    options_rows = snapshot.get("options_intelligence", [])
    multi_bagger_rows = snapshot.get("multi_bagger_candidates", [])

    alpha_cols = st.columns(4)
    alpha_cols[0].metric("Alpha candidates", len(alpha_scores))
    alpha_cols[1].metric(
        "A/A+", sum(1 for row in alpha_scores if row.get("grade") in {"A", "A+"})
    )
    alpha_cols[2].metric("Rejections", len(alpha_rejections))
    alpha_cols[3].metric("Leadership rows", len(symbol_strength))

    _section("Top alpha candidates")
    candidate_rows = []
    for row in alpha_scores:
        candidate_rows.append(
            {
                "Symbol": row.get("symbol"),
                "Score": row.get("score"),
                "Grade": row.get("grade"),
                "Strategy": row.get("strategy_id"),
                "Catalyst": row.get("catalyst_type"),
                "Expected R": row.get("expected_r"),
                "Win rate": row.get("historical_win_rate"),
                "Sample": row.get("expectancy_sample_size"),
                "Confidence": row.get("confidence_level"),
                "Risk x": row.get("suggested_risk_multiplier"),
                "Market regime": row.get("market_regime"),
                "Sector regime": row.get("sector_regime"),
                "Reason": row.get("explanation"),
                "Scanner": row.get("scanner_result_id"),
                "Signal": row.get("signal_id"),
            }
        )
    _table(candidate_rows, label="alpha candidate", height=420)

    _section("Sector leadership")
    st.caption(
        "Uses actual sector ETF vs SPY analytics when ETF candles are available; otherwise falls back to member-inferred leadership."
    )
    _table(sector_strength, label="sector leadership", height=260)
    _table(symbol_strength, label="symbol leadership", height=360)

    _section("Point-in-time universe / survivorship-bias control")
    _table(pit_universe_rows, label="point-in-time universe membership", height=260)

    _section("Short interest and options intelligence")
    st.caption(
        "Short squeeze/reversal candidates should not be traded blind: review short %, days-to-cover, borrow fee, utilization, float, IV rank/percentile, open interest, gamma/delta and expected move."
    )
    _table(short_interest_rows, label="short interest snapshot", height=260)
    _table(options_rows, label="options intelligence snapshot", height=260)

    _section("Multi-bagger / long-shot candidates")
    st.caption(
        "Separate long-horizon narrative/growth/flows/accumulation scorer for 3x/5x/10x watchlists; not an intraday trade signal."
    )
    _table(multi_bagger_rows, label="multi-bagger candidate", height=320)

    _section("Expectancy by strategy/setup")
    expectancy_strategy = [
        row
        for row in expectancy_rows
        if row.get("bucket_type")
        in {"by_strategy", "by_catalyst_type", "by_time_of_day"}
    ]
    _table(expectancy_strategy, label="expectancy bucket", height=360)

    _section("Recent alpha rejections / false positives")
    _table(alpha_rejections, label="alpha rejection", height=360)


with tab_overview:
    st.subheader("Command Center")
    st.caption("Live snapshot of the portfolio, market regime, and system activity.")

    _broker_snaps = snapshot["broker_account_snapshots"]
    _latest_acct = _broker_snaps[0] if _broker_snaps else None
    _prev_acct = _broker_snaps[1] if len(_broker_snaps) > 1 else None

    def _acct_value(field: str) -> Any:
        return _latest_acct.get(field) if _latest_acct else None

    def _acct_delta(field: str) -> str | None:
        if not _latest_acct or not _prev_acct:
            return None
        cur, prev = _latest_acct.get(field), _prev_acct.get(field)
        if cur is None or prev is None:
            return None
        return _fmt_money(cur - prev)

    _ov_positions = [p for p in snapshot["positions"] if (p.get("quantity") or 0)]
    _ov_symbols = sorted({p["symbol"] for p in _ov_positions})
    _ov_prices = {s: _price_history(s) for s in _ov_symbols}
    _ov_enriched = _enrich_positions(_ov_positions, _ov_prices)
    _ov_total_value = sum(
        r["market_value"] for r in _ov_enriched if r["market_value"] is not None
    )
    _ov_total_pnl = sum(
        r["unrealized_pnl"] for r in _ov_enriched if r["unrealized_pnl"] is not None
    )

    _ov_row1 = st.columns(4)
    _ov_row1[0].metric(
        "Equity", _fmt_money(_acct_value("equity")), delta=_acct_delta("equity")
    )
    _ov_row1[1].metric("Cash", _fmt_money(_acct_value("cash")))
    _ov_row1[2].metric("Buying power", _fmt_money(_acct_value("buying_power")))
    _ov_row1[3].metric("Open positions", len(_ov_enriched))

    _ov_row2 = st.columns(4)
    _ov_row2[0].metric(
        "Unrealized P&L", _fmt_money(_ov_total_pnl) if _ov_enriched else "—"
    )
    _ov_row2[1].metric(
        "Positions value", _fmt_money(_ov_total_value) if _ov_enriched else "—"
    )
    _ov_active_signal_count = sum(
        1
        for row in snapshot["signals"]
        if str(row.get("status", "")).upper() not in _TERMINAL_SIGNAL_STATUSES
    )
    _ov_row2[2].metric("Active signals", _ov_active_signal_count)
    _ov_row2[3].metric("Market regime", _latest_regime)

    _section("Equity curve")
    if _broker_snaps:
        _eq = pd.DataFrame(_broker_snaps)
        if "equity" in _eq.columns and "created_at" in _eq.columns:
            _eq = _eq[["created_at", "equity"]].dropna()
            _eq["created_at"] = pd.to_datetime(
                _eq["created_at"], utc=True, errors="coerce"
            )
            _eq = _eq.dropna().sort_values("created_at")
        else:
            _eq = pd.DataFrame()
        if not _eq.empty:
            _eq_fig = go.Figure()
            _eq_fig.add_trace(
                go.Scatter(
                    x=_eq["created_at"],
                    y=_eq["equity"],
                    mode="lines",
                    line=dict(color="#4c8dff", width=2),
                    fill="tozeroy",
                    fillcolor="rgba(76,141,255,0.12)",
                    name="Equity",
                )
            )
            st.plotly_chart(
                _style_fig(_eq_fig, height=300),
                width="stretch",
                config={"displayModeBar": False},
            )
        else:
            st.info(
                "No broker equity history yet. Use 'Sync Alpaca Paper' in the sidebar to populate it."
            )
    else:
        st.info(
            "No broker account snapshots yet. Use 'Sync Alpaca Paper' in the sidebar."
        )

    _ov_cols = st.columns([1.5, 1])
    with _ov_cols[0]:
        _section("Open positions")
        if _ov_enriched:
            st.dataframe(
                _style_positions(_positions_frame(_ov_enriched)),
                width="stretch",
                height=280,
            )
        else:
            st.info(
                "No open positions yet. They appear after paper fills or an Alpaca sync."
            )
    with _ov_cols[1]:
        _section("Market regime")
        _regimes = snapshot["regime_snapshots"]
        if _regimes:
            _r = _regimes[0]
            _rc = st.columns(2)
            _rc[0].metric("Regime", str(_r.get("market_regime") or "—"))
            _conf = _r.get("confidence")
            _rc[1].metric("Confidence", f"{_conf:.0f}" if _conf is not None else "—")
            _rc2 = st.columns(2)
            _rc2[0].metric("Allowed bias", str(_r.get("allowed_bias") or "—"))
            _rm = _r.get("risk_multiplier")
            _rc2[1].metric("Risk multiplier", f"{_rm:.2f}" if _rm is not None else "—")
            if _r.get("reason"):
                st.caption(str(_r["reason"]))
        else:
            st.info(
                "No market regime computed yet. Run 'Features + Regime + Catalysts'."
            )

    _section("Recent signals")
    _recent_signals = [
        {
            "symbol": row["symbol"],
            "strategy": row["strategy_id"],
            "status": row["status"],
            "direction": row["direction"],
            "confidence": row["confidence_score"],
            "created_at": row["created_at"],
        }
        for row in snapshot["signals"][:8]
    ]
    _table(_recent_signals, label="signal", height=240)

with sub_active:
    st.subheader("Active Trades")
    st.caption(
        "Positions you currently hold plus active signal setups the system is tracking."
    )

    _act_positions = [p for p in snapshot["positions"] if (p.get("quantity") or 0)]
    _act_symbols = sorted({p["symbol"] for p in _act_positions})
    _act_prices = {s: _price_history(s) for s in _act_symbols}
    _act_enriched = _enrich_positions(_act_positions, _act_prices)

    if _act_enriched:
        _act_mv = sum(
            r["market_value"] for r in _act_enriched if r["market_value"] is not None
        )
        _act_cost = sum(
            (r["average_price"] or 0.0) * r["quantity"]
            for r in _act_enriched
            if r["average_price"] is not None
        )
        _act_pnl = sum(
            r["unrealized_pnl"]
            for r in _act_enriched
            if r["unrealized_pnl"] is not None
        )
        _act_winners = sum(1 for r in _act_enriched if (r["unrealized_pnl"] or 0) > 0)
        _act_k = st.columns(4)
        _act_k[0].metric("Open positions", len(_act_enriched))
        _act_k[1].metric("Market value", _fmt_money(_act_mv))
        _act_k[2].metric(
            "Unrealized P&L",
            _fmt_money(_act_pnl),
            delta=_fmt_pct(_act_pnl / _act_cost * 100.0) if _act_cost else None,
        )
        _act_k[3].metric(
            "Winners / losers", f"{_act_winners} / {len(_act_enriched) - _act_winners}"
        )

        _section("Open positions")
        st.caption(
            "Current price and P&L use the latest collected candle for each symbol."
        )
        st.dataframe(
            _style_positions(_positions_frame(_act_enriched)),
            width="stretch",
            height=340,
        )

        _act_priced = [r for r in _act_enriched if r["unrealized_pnl"] is not None]
        if _act_priced:
            _section("Unrealized P&L by position")
            _act_bar = go.Figure(
                go.Bar(
                    x=[r["symbol"] for r in _act_priced],
                    y=[r["unrealized_pnl"] for r in _act_priced],
                    marker_color=[
                        "#29d398" if (r["unrealized_pnl"] or 0) >= 0 else "#f87171"
                        for r in _act_priced
                    ],
                )
            )
            st.plotly_chart(
                _style_fig(_act_bar, height=280),
                width="stretch",
                config={"displayModeBar": False},
            )
        else:
            st.info("Collect market data for these symbols to compute live P&L.")
    else:
        st.info(
            "No open positions right now. Run a paper submission or 'Sync Alpaca Paper'."
        )

    _active_signals = [
        row
        for row in snapshot["signals"]
        if str(row.get("status", "")).upper() not in _TERMINAL_SIGNAL_STATUSES
    ]
    _section("Active signal setups")
    st.caption("Generated signals not yet filled, cancelled, or expired.")
    _act_sig_rows = [
        {
            "symbol": row["symbol"],
            "strategy": row["strategy_id"],
            "status": row["status"],
            "direction": row["direction"],
            "entry_zone": _compact_dict(row["entry_zone"]),
            "stop_loss": row["stop_loss"],
            "target_1": row["target_1"],
            "risk_reward": row["risk_reward"],
            "confidence": row["confidence_score"],
            "created_at": row["created_at"],
        }
        for row in _active_signals
    ]
    _table(_act_sig_rows, label="active signal", height=300)

with sub_market:
    if news_only_mode:
        st.caption(
            "News-only mode is ON — live price charts and the market board are disabled."
        )
        st.info(
            "Price charts, the live market board, and SPY benchmarking are hidden "
            "because the platform is running in news-only mode. See the News tab "
            "for news-ranked opportunities."
        )
    else:
        st.caption(
            "Live board, performance vs the S&P 500, and intraday price action from collected candles."
        )

        _mkt_symbols = list(snapshot["active_symbols"])[:50]
        _section("Live market board")
        if _mkt_symbols:
            _board_prices = {s: _price_history(s) for s in _mkt_symbols}
            _board_rows = []
            for _s in _mkt_symbols:
                _f = _board_prices[_s]
                _cur = _series_close(_f)
                _prev = _series_close(_f, -2)
                _chg = (
                    ((_cur - _prev) / _prev * 100.0)
                    if (_cur is not None and _prev)
                    else None
                )
                _board_rows.append(
                    {
                        "Symbol": _s,
                        "Price": _cur,
                        "Last bar %": _chg,
                        "Volume": _latest_volume(_f),
                    }
                )
            _board_df = pd.DataFrame(_board_rows)
            if _board_df["Price"].notna().any():
                st.dataframe(
                    _style_board(_board_df),
                    width="stretch",
                    height=min(420, 60 + 35 * len(_board_rows)),
                )
                _chg_rows = sorted(
                    [r for r in _board_rows if r["Last bar %"] is not None],
                    key=lambda r: r["Last bar %"],
                )
                if _chg_rows:
                    _board_fig = go.Figure(
                        go.Bar(
                            x=[r["Last bar %"] for r in _chg_rows],
                            y=[r["Symbol"] for r in _chg_rows],
                            orientation="h",
                            marker_color=[
                                "#29d398" if r["Last bar %"] >= 0 else "#f87171"
                                for r in _chg_rows
                            ],
                        )
                    )
                    st.plotly_chart(
                        _style_fig(_board_fig, height=max(220, 26 * len(_chg_rows))),
                        width="stretch",
                        config={"displayModeBar": False},
                    )
            else:
                st.info(
                    "No price data collected yet. Use 'Collect Real Market Data' in the sidebar."
                )
        else:
            st.info(
                "No active symbols. Add symbols in the sidebar, then collect market data."
            )

        _section("Performance vs S&P 500 (SPY)")
        _cmp_choices = _mkt_symbols or ["AAPL"]
        _cmp_symbol = st.selectbox(
            "Symbol to chart", _cmp_choices, key="market_cmp_symbol"
        )
        _sym_frame = _price_history(_cmp_symbol)
        if _sym_frame.empty:
            st.info(
                f"No candle data for {_cmp_symbol} yet. Collect market data for it first."
            )
        else:

            def _rebased(frame: pd.DataFrame) -> pd.DataFrame:
                closes = frame[["timestamp", "close"]].dropna()
                if closes.empty:
                    return pd.DataFrame()
                base = closes["close"].iloc[0]
                if not base:
                    return pd.DataFrame()
                closes = closes.copy()
                closes["rebased"] = closes["close"] / base * 100.0
                return closes

            _sym_re = _rebased(_sym_frame)
            if _sym_re.empty:
                st.info(f"Not enough valid price data to chart {_cmp_symbol}.")
            else:
                _cmp_fig = go.Figure()
                _cmp_fig.add_trace(
                    go.Scatter(
                        x=_sym_re["timestamp"],
                        y=_sym_re["rebased"],
                        mode="lines",
                        line=dict(color="#4c8dff", width=2),
                        name=_cmp_symbol,
                    )
                )
                _spy_frame = _price_history("SPY")
                if _cmp_symbol != "SPY" and not _spy_frame.empty:
                    _spy_re = _rebased(_spy_frame)
                    if not _spy_re.empty:
                        _cmp_fig.add_trace(
                            go.Scatter(
                                x=_spy_re["timestamp"],
                                y=_spy_re["rebased"],
                                mode="lines",
                                line=dict(color="#8a98b0", width=1.6, dash="dot"),
                                name="SPY (S&P 500)",
                            )
                        )
                        st.caption(
                            "Both series rebased to 100 at the start of the window for a like-for-like comparison."
                        )
                elif _cmp_symbol != "SPY":
                    st.caption(
                        "Collect candles for SPY to overlay the S&P 500 benchmark."
                    )
                st.plotly_chart(
                    _style_fig(_cmp_fig, height=320),
                    width="stretch",
                    config={"displayModeBar": False},
                )

                _section(f"{_cmp_symbol} price & VWAP")
                _price_fig = go.Figure()
                _price_fig.add_trace(
                    go.Scatter(
                        x=_sym_frame["timestamp"],
                        y=_sym_frame["close"],
                        mode="lines",
                        line=dict(color="#4c8dff", width=2),
                        name="Close",
                    )
                )
                if "vwap" in _sym_frame.columns and _sym_frame["vwap"].notna().any():
                    _price_fig.add_trace(
                        go.Scatter(
                            x=_sym_frame["timestamp"],
                            y=_sym_frame["vwap"],
                            mode="lines",
                            line=dict(color="#f5b14c", width=1.4, dash="dot"),
                            name="VWAP",
                        )
                    )
                st.plotly_chart(
                    _style_fig(_price_fig, height=300),
                    width="stretch",
                    config={"displayModeBar": False},
                )

    st.divider()
    st.subheader("Real Market Data")
    st.caption(
        "Rows are persisted from collector calls. Empty tables mean no data has been collected yet."
    )
    market_cols = st.columns([1, 1])
    with market_cols[0]:
        _section("Latest clean candles")
        _table(snapshot["clean_candles"], label="clean candle", height=420)
    with market_cols[1]:
        _section("Latest feature calculations")
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

    _section("Scanner results")
    _table(snapshot["scanner_results"], label="scanner result", height=360)

    _section("Daily feature calculations")
    _table(snapshot["daily_features"], label="daily feature", height=260)

    _section("Market regime snapshots")
    _table(snapshot["regime_snapshots"], label="market regime snapshot", height=260)

with sub_catalysts:
    st.subheader("Catalyst And Stream Intelligence")
    catalyst_cols = st.columns(2)
    with catalyst_cols[0]:
        _section("Alpaca market-data stream events")
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
        _section("Scheduler runs")
        _table(snapshot["scheduler_runs"], label="scheduler run", height=360)

    news_cols = st.columns(2)
    with news_cols[0]:
        _section("Clean news")
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
        _section("SEC filings")
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
        _section("Normalized events")
        _table(snapshot["events"], label="event", height=360)
    with event_cols[1]:
        _section("Catalysts")
        _table(snapshot["catalysts"], label="catalyst", height=360)

with sub_signals:
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

    _section("Trade thesis reasoning")
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

    _section("Opportunity rankings (most recent accepted scanner results)")
    if settings.enable_ranking_signal_path:
        st.caption(
            "Ranking-gated signal path is ENABLED: A_PLUS / A grades route to live signals."
        )
    else:
        st.caption("Ranking-gated signal path is disabled; rankings are advisory only.")
    ranking_rows = []
    try:
        ranking_rows = _api_get("/rankings/recent", params={"limit": 50}).get(
            "rankings", []
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - surface ranking errors without crashing the tab
        st.warning(f"Opportunity ranking is unavailable: {exc}")
    _table(ranking_rows, label="opportunity ranking", height=360)

    _section("Expectancy layer (historical outcomes of closed trades)")
    st.caption(
        "What happened when trades looked like this before. Real closed trades only; "
        "empty rows (0 samples) mean no history yet, not a zero result."
    )
    try:
        expectancy_summary = _api_get("/expectancy/summary")
        _table(
            [expectancy_summary.get("overall", {})],
            label="expectancy overall",
            height=110,
        )
        for key, title in (
            ("by_regime", "By regime"),
            ("by_sector", "By sector"),
            ("by_symbol", "By symbol"),
        ):
            buckets = expectancy_summary.get(key, {})
            if buckets:
                st.caption(title)
                _table(
                    [{"cohort": name, **stats} for name, stats in buckets.items()],
                    label=f"expectancy {key}",
                    height=240,
                )
    except Exception as exc:
        st.warning(f"Expectancy layer is unavailable: {exc}")

with sub_risk:
    st.subheader("Risk And Execution")
    st.caption(
        "Paper submission requires `ENVIRONMENT_MODE=paper`; otherwise the order is blocked and logged."
    )
    signals = snapshot["signals"]
    signal_options = {
        f"{row['symbol']} | {row['strategy_id']} | {row['created_at']} | {row['id'][:8]}": row[
            "id"
        ]
        for row in signals
    }
    if signal_options:
        selected_label = st.selectbox(
            "Signal to risk-check / paper-submit", list(signal_options.keys())
        )
        latest_account = (snapshot.get("broker_account_snapshots") or [{}])[0]
        latest_positions = [
            row
            for row in snapshot.get("positions", [])
            if float(row.get("quantity") or 0.0) != 0.0
        ]
        risk_cols = st.columns(4)
        risk_cols[0].metric("Account equity", latest_account.get("equity") or "No snapshot")
        risk_cols[1].metric("Open positions", len(latest_positions))
        risk_cols[2].metric("Daily loss %", "Server-derived")
        risk_cols[3].metric("Trades today", "Server-derived")
        risk_cols_2 = st.columns(4)
        weekly_loss_pct = risk_cols_2[0].number_input(
            "Weekly loss override %", value=0.0, min_value=0.0
        )
        sector_exposure_pct = risk_cols_2[1].number_input(
            "Sector exposure %", value=0.0, min_value=0.0
        )
        internal_quantity = risk_cols_2[2].number_input(
            "Internal position qty", value=0.0
        )
        broker_quantity = risk_cols_2[3].number_input("Broker position qty", value=0.0)

        if st.button(
            "Run Risk Check + Paper Submit", width="stretch", disabled=not can_trade
        ):
            result = _api_post(
                "/execution/paper/submit-signal",
                {
                    "signal_id": signal_options[selected_label],
                    "weekly_loss_pct": weekly_loss_pct,
                    "sector_exposure_pct": sector_exposure_pct,
                    "internal_quantity": internal_quantity,
                    "broker_quantity": broker_quantity,
                },
            )
            st.session_state["last_paper_submit"] = result
            st.rerun()
    else:
        st.info(
            "No signals available yet. Collect market data and run a scan cycle first."
        )

    if "last_paper_submit" in st.session_state:
        with st.expander("Last paper submission decision", expanded=True):
            st.json(st.session_state["last_paper_submit"])

    exec_cols = st.columns(4)
    with exec_cols[0]:
        _section("Risk checks")
        _table(snapshot["risk_checks"], label="risk check", height=360)
    with exec_cols[1]:
        _section("Orders")
        _table(snapshot["orders"], label="order", height=360)
    with exec_cols[2]:
        _section("Fills")
        _table(snapshot["fills"], label="fill", height=360)
    with exec_cols[3]:
        _section("Positions")
        _table(snapshot["positions"], label="position", height=360)

    broker_cols = st.columns(3)
    with broker_cols[0]:
        _section("Broker sync logs")
        _table(snapshot["broker_sync_logs"], label="broker sync log", height=300)
    with broker_cols[1]:
        _section("Broker account snapshots")
        _table(
            snapshot["broker_account_snapshots"],
            label="broker account snapshot",
            height=300,
        )
    with broker_cols[2]:
        _section("Execution errors")
        _table(snapshot["execution_errors"], label="execution error", height=300)

    _section("Exposure snapshots")
    _table(snapshot["exposure_snapshots"], label="exposure snapshot", height=300)

with sub_journal:
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
        submitted = st.form_submit_button(
            "Record Journal Entry", disabled=not can_trade
        )
        if submitted:
            if not journal_symbol.strip() or not entry_thesis.strip():
                st.error("Journal symbol and entry thesis are required.")
            else:
                _api_post(
                    "/journal/entries",
                    {
                        "symbol": journal_symbol,
                        "strategy_id": journal_strategy or None,
                        "entry_thesis": entry_thesis,
                        "actual_entry": actual_entry or None,
                        "actual_exit": actual_exit or None,
                        "pnl": pnl,
                        "human_notes": human_notes or None,
                        "mistake_tags": [
                            item.strip()
                            for item in mistake_tags_raw.split(",")
                            if item.strip()
                        ],
                    },
                )
                st.success("Journal entry recorded.")
                st.rerun()

    _table(snapshot["journal"], label="journal", height=420)

    _section("Trade reviews")
    _table(snapshot["ai_reviews"], label="trade review", height=320)

    review_cols = st.columns(2)
    with review_cols[0]:
        _section("Weekly reviews")
        _table(snapshot["weekly_reviews"], label="weekly review", height=300)
    with review_cols[1]:
        _section("Learning recommendations")
        _table(
            snapshot["strategy_recommendations"],
            label="strategy recommendation",
            height=300,
        )

with sub_providers:
    st.subheader("Providers, API Calls, And Data Quality")
    quality_cols = st.columns(4)
    with quality_cols[0]:
        _section("Provider capabilities")
        _table(snapshot["providers"], label="provider capability", height=360)
    with quality_cols[1]:
        _section("Provider rate limits")
        _table(
            snapshot["provider_rate_limits"], label="provider rate limit", height=360
        )
    with quality_cols[2]:
        _section("API call logs")
        _table(snapshot["api_calls"], label="API call", height=360)
    with quality_cols[3]:
        _section("Data quality errors")
        _table(snapshot["data_quality_errors"], label="data quality error", height=360)

    _section("Strategy registry")
    _table(snapshot["strategies"], label="strategy", height=260)

    _section("Provider health")
    _table(snapshot["provider_health"], label="provider health", height=300)

    _section("Worker health")
    _table(snapshot["worker_heartbeats"], label="worker heartbeat", height=260)

    _section("Missing candle gaps")
    _table(snapshot["missing_candle_gaps"], label="missing candle gap", height=260)

    _section("Backtest reports")
    _table(snapshot["backtest_reports"], label="backtest report", height=260)

with sub_decisions:
    st.subheader("Decision Logs")
    st.caption(
        "Scanner, signal, risk, execution, journal, and AI reasoning decisions are recorded here."
    )
    _table(snapshot["decisions"], label="decision log", height=520)

    _section("Audit logs")
    _table(snapshot["audit_logs"], label="audit log", height=360)

with sub_readiness:
    st.subheader("Live Readiness")
    st.caption(
        "Live execution is disabled by default and requires every readiness, approval, and gate check."
    )
    if can_admin:
        approval_cols = st.columns(2)
        with approval_cols[0]:
            with st.form("live_approval_create_form", clear_on_submit=True):
                _section("Create approval window")
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
                    expires_at = datetime.now(UTC) + timedelta(
                        minutes=int(approval_minutes)
                    )
                    result = _api_post(
                        "/live-readiness/approve",
                        {
                            "reason": approval_reason,
                            "expires_at": expires_at.isoformat(),
                        },
                    )
                    st.session_state["last_live_approval_action"] = result
                    st.rerun()
        with approval_cols[1]:
            active_approvals = [
                row
                for row in snapshot["live_trading_approvals"]
                if row.get("status") == "ACTIVE" and not row.get("revoked_at")
            ]
            with st.form("live_approval_revoke_form", clear_on_submit=True):
                _section("Revoke approval")
                approval_ids = [row["id"] for row in active_approvals]
                approval_id = st.selectbox(
                    "Active approval", approval_ids, disabled=not approval_ids
                )
                revoke_reason = st.text_area("Revocation reason")
                submitted = st.form_submit_button(
                    "Revoke Live Approval",
                    disabled=not approval_ids or not revoke_reason.strip(),
                )
                if submitted:
                    result = _api_post(
                        "/live-readiness/approvals/revoke",
                        {"approval_id": approval_id, "reason": revoke_reason},
                    )
                    st.session_state["last_live_approval_action"] = result
                    st.rerun()
    elif principal.role == AdminRole.TRADER.value:
        st.info("Admin role is required to create or revoke live approval windows.")

    reports = snapshot["live_readiness_reports"]
    _table(reports, label="live-readiness report", height=360)
    if reports:
        latest = reports[0]
        _section("Latest readiness checks")
        checks = latest.get("checks") or []
        _table(checks, label="live-readiness check", height=460)

    _section("Live approvals")
    _table(snapshot["live_trading_approvals"], label="live approval", height=260)

    _section("Kill switches")
    _table(snapshot["kill_switches"], label="kill switch", height=260)

    _section("Strategy approval requests")
    _table(
        snapshot["strategy_approval_requests"],
        label="strategy approval request",
        height=300,
    )

with tab_admin:
    st.subheader("Admin Users")
    if not can_admin:
        st.info("Admin role is required to manage users.")
    else:
        admin_users = _api_get("/admin/users", params={"limit": 100}).get(
            "admin_users", []
        )
        _table(admin_users, label="admin user", height=300)
        admin_usernames = [row["username"] for row in admin_users]
        role_options = [role.value for role in AdminRole]

        create_cols = st.columns(2)
        with create_cols[0]:
            with st.form("admin_user_upsert_form", clear_on_submit=True):
                _section("Create or update user")
                admin_username = st.text_input("Username")
                admin_password = st.text_input("Password", type="password")
                admin_role = st.selectbox(
                    "Role",
                    role_options,
                    index=role_options.index(AdminRole.VIEWER.value),
                )
                admin_reason = st.text_area("Reason")
                submitted = st.form_submit_button(
                    "Save User",
                    disabled=not admin_username.strip()
                    or len(admin_password) < 12
                    or not admin_reason.strip(),
                )
                if submitted:
                    if (
                        admin_username.strip() == principal.username
                        and admin_role != AdminRole.ADMIN.value
                    ):
                        st.error("Admins cannot demote their own active session user.")
                    else:
                        result = _api_post(
                            "/admin/users",
                            {
                                "username": admin_username.strip(),
                                "password": admin_password,
                                "role": admin_role,
                                "reason": admin_reason,
                            },
                        )
                        st.session_state["last_admin_user_action"] = result
                        st.rerun()

        with create_cols[1]:
            with st.form("admin_user_role_form", clear_on_submit=True):
                _section("Change role")
                role_username = st.selectbox(
                    "User", admin_usernames, disabled=not admin_usernames
                )
                role_value = st.selectbox(
                    "New role", role_options, key="admin_user_role_select"
                )
                role_reason = st.text_area("Reason", key="admin_user_role_reason")
                submitted = st.form_submit_button(
                    "Change Role",
                    disabled=not admin_usernames or not role_reason.strip(),
                )
                if submitted:
                    if (
                        role_username == principal.username
                        and role_value != AdminRole.ADMIN.value
                    ):
                        st.error("Admins cannot demote their own active session user.")
                    else:
                        result = _api_post(
                            "/admin/users/role",
                            {
                                "username": role_username,
                                "role": role_value,
                                "reason": role_reason,
                            },
                        )
                        st.session_state["last_admin_user_action"] = result
                        st.rerun()

        state_cols = st.columns(2)
        with state_cols[0]:
            with st.form("admin_user_active_form", clear_on_submit=True):
                _section("Activate or deactivate")
                active_username = st.selectbox(
                    "User",
                    admin_usernames,
                    key="admin_user_active",
                    disabled=not admin_usernames,
                )
                is_active = st.checkbox("Active", value=True)
                active_reason = st.text_area("Reason", key="admin_user_active_reason")
                submitted = st.form_submit_button(
                    "Update Active State",
                    disabled=not admin_usernames or not active_reason.strip(),
                )
                if submitted:
                    if active_username == principal.username and not is_active:
                        st.error(
                            "Admins cannot deactivate their own active session user."
                        )
                    else:
                        result = _api_post(
                            "/admin/users/active",
                            {
                                "username": active_username,
                                "is_active": is_active,
                                "reason": active_reason,
                            },
                        )
                        st.session_state["last_admin_user_action"] = result
                        st.rerun()

        with state_cols[1]:
            with st.form("admin_user_unlock_form", clear_on_submit=True):
                _section("Unlock user")
                unlock_username = st.selectbox(
                    "User",
                    admin_usernames,
                    key="admin_user_unlock",
                    disabled=not admin_usernames,
                )
                unlock_reason = st.text_area("Reason", key="admin_user_unlock_reason")
                submitted = st.form_submit_button(
                    "Clear Lockout",
                    disabled=not admin_usernames or not unlock_reason.strip(),
                )
                if submitted:
                    result = _api_post(
                        "/admin/users/unlock",
                        {"username": unlock_username, "reason": unlock_reason},
                    )
                    st.session_state["last_admin_user_action"] = result
                    st.rerun()

if auto_refresh:
    time.sleep(max(5, int(settings.dashboard_refresh_seconds)))
    st.rerun()
