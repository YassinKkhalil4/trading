from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
LEGACY_DASHBOARD = ROOT / "trading_system" / "dashboard"


def _read_frontend() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in FRONTEND.rglob("*.ts*") if path.is_file())


def test_streamlit_dashboard_is_removed_and_sunset_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not LEGACY_DASHBOARD.exists()
    assert "2026-06-28" in readme
    assert "Do not add new Streamlit entrypoints" in readme


def test_nextjs_exposes_audited_admin_user_management_and_operations():
    source = _read_frontend()
    required_fragments = [
        "Admin Users",
        "listAdminUsers",
        "upsertAdminUser",
        "setAdminUserRole",
        "setAdminUserActive",
        "clearAdminUserLockout",
        "admin_auth_token",
        "revokes sessions",
        "blocks self-demotion/deactivation",
        "dashboard_sync_alpaca_paper",
        "dashboard_reconcile_fills",
        "dashboard_run_alpaca_stream_batch",
        "dashboard_run_production_scanners",
        "dashboard_generate_live_readiness_report",
        "Manual Operations",
        "MANUAL_OPERATION_RUN",
        "Run Alpaca Stream Batch",
        "Last {selected?.label} result",
    ]

    for fragment in required_fragments:
        assert fragment in source, fragment


def test_dashboard_does_not_label_real_trading_views_as_sample_data():
    source = _read_frontend()

    assert "Run Alpaca Stream Batch" in source
    assert "sample-data path" in source
    assert "Run Alpaca Stream Sample" not in source
    assert "Last Alpaca stream sample result" not in source
    assert "dashboard_run_alpaca_stream_sample" not in source
