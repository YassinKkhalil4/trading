from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_APP = ROOT / "trading_system" / "dashboard" / "app.py"


def test_dashboard_exposes_audited_admin_user_management():
    source = DASHBOARD_APP.read_text(encoding="utf-8")
    required_fragments = [
        '"Admin"',
        "Admin Users",
        "hash_password",
        "list_admin_users",
        "upsert_admin_user",
        "set_admin_user_role",
        "set_admin_user_active",
        "clear_admin_user_lockout",
        "revoke_admin_sessions_for_user",
        "revoked_sessions",
        "ADMIN_USER_UPSERTED",
        "ADMIN_USER_ROLE_CHANGED",
        "ADMIN_USER_ACTIVE_CHANGED",
        "ADMIN_USER_UNLOCKED",
        "Admins cannot demote their own active session user.",
        "Admins cannot deactivate their own active session user.",
        "_audit_symbol_config",
        "SYMBOL_ACTIVATED",
        "SYMBOL_DEACTIVATED",
        "SYMBOL_ACTIVATED_FOR_COLLECTION",
        "SYMBOL_ACTIVATED_FOR_SCAN",
        "Deactivate Symbol",
        "deactivate_symbol",
        'entity_type="symbol_universe"',
        "generate_live_readiness_report(actor=principal.username)",
        "run_scheduled_job(",
        "actor=principal.username",
        "_audit_manual_operation",
        "MANUAL_OPERATION_RUN",
        "dashboard_sync_alpaca_paper",
        "dashboard_reconcile_fills",
        "dashboard_run_alpaca_stream_batch",
        "dashboard_run_production_scanners",
        "dashboard_generate_live_readiness_report",
        "Exposure snapshots",
        'snapshot["exposure_snapshots"]',
        "Broker sync logs",
        "Broker account snapshots",
        "Execution errors",
        "Worker health",
        "Missing candle gaps",
        "Backtest reports",
        "Live approvals",
        "Strategy approval requests",
        "Learning recommendations",
    ]

    for fragment in required_fragments:
        assert fragment in source, fragment


def test_dashboard_does_not_label_real_trading_views_as_sample_data():
    source = DASHBOARD_APP.read_text(encoding="utf-8")

    assert "Run Alpaca Stream Batch" in source
    assert "Last Alpaca stream batch result" in source
    assert "Run Alpaca Stream Sample" not in source
    assert "Last Alpaca stream sample result" not in source
    assert "dashboard_run_alpaca_stream_sample" not in source
