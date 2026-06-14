from __future__ import annotations

from datetime import UTC, datetime
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from types import SimpleNamespace

from trading_system.app.api.main import app
from trading_system.app.api.routers import admin as admin_router
from trading_system.app.api.routers import common as common_router
from trading_system.app.api.routers import execution as execution_router
from trading_system.app.api.routers import market as market_router
from trading_system.app.services.ranking.expectancy import ExpectancyStats, empty_stats
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityGrade,
    OpportunityRankingResult,
)
from trading_system.app.security.auth import AdminPrincipal
from trading_system.app.security.auth import (
    require_admin_token,
    require_principal,
    require_trader_or_admin,
)


client = TestClient(app)


def test_cors_is_restricted_to_configured_origins():
    cors_middleware = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )

    assert cors_middleware.kwargs["allow_origins"] == ["https://trading.example.com"]
    assert "*" not in cors_middleware.kwargs["allow_origins"]


def test_read_routes_are_protected_by_default():
    public_get_routes = {
        "/health",
    }
    auth_dependencies = {require_principal, require_admin_token, require_trader_or_admin}
    unprotected = []

    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if route.path in public_get_routes:
            continue
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        if not dependency_calls.intersection(auth_dependencies):
            unprotected.append(route.path)

    assert unprotected == []


def test_public_health_endpoint_remains_available():
    response = client.get("/health")

    assert response.status_code == 200
    assert "environment_mode" not in response.json()
    assert "live_order_path_enabled" not in response.json()


def test_database_bootstrap_requires_admin_authentication():
    response = client.post("/db/bootstrap")

    assert response.status_code == 401


def test_sensitive_read_endpoints_require_authentication():
    endpoints = [
        "/dashboard/snapshot",
        "/admin/users",
        "/ops/health",
        "/ops/workers",
        "/environment",
        "/provider-capabilities",
        "/strategies",
        "/universe",
        "/market/clean-candles",
        "/features/latest",
        "/features/daily",
        "/regime/snapshots",
        "/catalysts/events",
        "/catalysts/scores",
        "/scanners/results",
        "/rankings/recent",
        "/expectancy/summary",
        "/signals",
        "/signals/theses",
        "/risk/checks",
        "/risk/exposures",
        "/broker/account-snapshots",
        "/broker/sync-logs",
        "/providers/health",
        "/providers/rate-limits",
        "/streams/events",
        "/scheduler/runs",
        "/data/news",
        "/data/sec-filings",
        "/data/quality-errors",
        "/data/missing-candle-gaps",
        "/execution/orders",
        "/execution/fills",
        "/execution/positions",
        "/execution/errors",
        "/journal/entries",
        "/reviews/trades",
        "/reviews/weekly",
        "/learning/recommendations",
        "/backtests/reports",
        "/strategy-approvals/requests",
        "/kill-switches",
        "/decisions",
        "/audit/logs",
        "/live-readiness/reports",
        "/live-readiness/approvals",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 401, endpoint


def test_api_exposes_required_dashboard_and_operations_surfaces():
    route_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
    }
    required_paths = {
        "/ops/health",
        "/ops/workers",
        "/provider-capabilities",
        "/providers/health",
        "/providers/rate-limits",
        "/universe",
        "/market/clean-candles",
        "/features/latest",
        "/features/daily",
        "/regime/snapshots",
        "/catalysts/events",
        "/catalysts/scores",
        "/scanners/results",
        "/rankings/recent",
        "/expectancy/summary",
        "/signals",
        "/signals/theses",
        "/risk/checks",
        "/risk/exposures",
        "/broker/account-snapshots",
        "/broker/sync-logs",
        "/execution/orders",
        "/execution/fills",
        "/execution/positions",
        "/execution/errors",
        "/journal/entries",
        "/reviews/trades",
        "/reviews/weekly",
        "/learning/recommendations",
        "/backtests/reports",
        "/strategy-approvals/requests",
        "/live-readiness/reports",
        "/live-readiness/approvals",
        "/kill-switches",
        "/decisions",
        "/audit/logs",
        "/data/quality-errors",
        "/data/missing-candle-gaps",
        "/streams/events",
        "/scheduler/runs",
    }

    assert required_paths <= route_paths


def test_operational_mutation_endpoints_require_authentication():
    endpoints_and_payloads = [
        ("/symbols/activate", {"symbol": "AMD", "reason": "test"}),
        ("/symbols/deactivate", {"symbol": "AMD", "reason": "test"}),
        ("/symbols/tradability", {"symbol": "AMD", "is_tradable": False, "reason": "test"}),
        ("/collect/yahoo", {"symbols": ["AMD"]}),
        (
            "/admin/users",
            {
                "username": "viewer",
                "password": "very-secure-password",
                "role": "VIEWER",
                "reason": "test",
            },
        ),
        ("/admin/users/role", {"username": "viewer", "role": "TRADER", "reason": "test"}),
        ("/admin/users/active", {"username": "viewer", "is_active": True, "reason": "test"}),
        ("/admin/users/unlock", {"username": "viewer", "reason": "test"}),
        ("/collect/alpaca-bars", {"symbols": ["AMD"]}),
        ("/scan/watchlist", {"symbols": ["AMD"]}),
        ("/scanners/vwap-reclaim", {}),
        ("/risk/check-vwap-reclaim", {}),
        ("/broker/alpaca-paper/sync", {}),
        ("/provider-health/run", {}),
        ("/live-readiness/report", {}),
        ("/live-readiness/approvals/revoke", {"approval_id": "missing", "reason": "test"}),
        ("/execution/paper/submit-vwap-reclaim", {}),
        ("/execution/orders/replace", {"order_id": "missing", "reason": "test"}),
        ("/execution/orders/submit-broker", {"order_id": "missing", "reason": "test"}),
    ]

    for endpoint, payload in endpoints_and_payloads:
        response = client.post(endpoint, json=payload)
        assert response.status_code == 401, endpoint


def test_state_changing_routes_are_protected_by_default():
    public_post_routes = {
        "/auth/login",
    }
    auth_dependencies = {require_principal, require_admin_token, require_trader_or_admin}
    unprotected = []

    for route in app.routes:
        if not isinstance(route, APIRoute) or "POST" not in route.methods:
            continue
        if route.path in public_post_routes:
            continue
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        if not dependency_calls.intersection(auth_dependencies):
            unprotected.append(route.path)

    assert unprotected == []


def test_live_approval_revoke_endpoint_is_admin_only_and_uses_repository(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def __init__(self) -> None:
            self.revoked: dict | None = None

        def revoke_live_trading_approval(self, *, approval_id: str, revoked_by: str, reason: str):
            self.revoked = {
                "approval_id": approval_id,
                "revoked_by": revoked_by,
                "reason": reason,
            }

        def latest_live_trading_approvals(self, limit: int = 20) -> list[dict]:
            return [
                {
                    "id": self.revoked["approval_id"] if self.revoked else "unknown",
                    "status": "REVOKED",
                    "revoked_by": self.revoked["revoked_by"] if self.revoked else None,
                    "revoke_reason": self.revoked["reason"] if self.revoked else None,
                }
            ][:limit]

    class FakeService:
        def __init__(self, repository: FakeRepository) -> None:
            self.repository = repository

        def bootstrap(self) -> dict:
            return {}

    repository = FakeRepository()

    def fake_runtime():
        return FakeSession(), FakeService(repository)

    monkeypatch.setattr(admin_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_admin_token] = lambda: AdminPrincipal(username="risk-admin", role="ADMIN")
    try:
        response = client.post(
            "/live-readiness/approvals/revoke",
            json={"approval_id": "approval-1", "reason": "Operator ended the live window."},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert repository.revoked == {
        "approval_id": "approval-1",
        "revoked_by": "risk-admin",
        "reason": "Operator ended the live window.",
    }
    assert response.json()["approval"]["status"] == "REVOKED"


def test_symbol_config_mutations_are_audited(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def __init__(self) -> None:
            self.row = SimpleNamespace(
                id="symbol-1",
                symbol="AMD",
                sector="Semiconductors",
                is_active=True,
                is_tradable=True,
                change_reason="",
                tradability_reason=None,
            )
            self.audit_events: list[dict] = []

        def add_or_activate_symbol(self, symbol: str, *, name=None, sector=None, reason: str):
            self.row.symbol = symbol.upper()
            self.row.sector = sector
            self.row.change_reason = reason
            return self.row

        def set_symbol_tradability(self, symbol: str, *, is_tradable: bool, reason: str):
            if symbol.upper() != self.row.symbol:
                return None
            self.row.is_tradable = is_tradable
            self.row.tradability_reason = reason
            self.row.change_reason = reason
            return self.row

        def deactivate_symbol(self, symbol: str, reason: str):
            if symbol.upper() != self.row.symbol:
                return None
            self.row.is_active = False
            self.row.change_reason = reason
            return self.row

        def store_audit_log(self, **kwargs):
            self.audit_events.append(kwargs)

    class FakeService:
        def __init__(self, repository: FakeRepository) -> None:
            self.repository = repository

        def bootstrap(self) -> dict:
            return {}

    repository = FakeRepository()

    def fake_runtime():
        return FakeSession(), FakeService(repository)

    monkeypatch.setattr(market_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_trader_or_admin] = lambda: AdminPrincipal(
        username="trader",
        role="TRADER",
    )
    try:
        activated = client.post(
            "/symbols/activate",
            json={
                "symbol": "amd",
                "sector": "Semiconductors",
                "reason": "Add AMD to operator universe.",
            },
        )
        tradability = client.post(
            "/symbols/tradability",
            json={
                "symbol": "AMD",
                "is_tradable": False,
                "reason": "Spread too wide today.",
            },
        )
        deactivated = client.post(
            "/symbols/deactivate",
            json={
                "symbol": "AMD",
                "reason": "Remove from active universe.",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert activated.status_code == 200
    assert tradability.status_code == 200
    assert deactivated.status_code == 200
    assert [event["event_type"] for event in repository.audit_events] == [
        "SYMBOL_ACTIVATED",
        "SYMBOL_TRADABILITY_CHANGED",
        "SYMBOL_DEACTIVATED",
    ]
    assert all(event["actor"] == "trader" for event in repository.audit_events)
    assert all(event["entity_type"] == "symbol_universe" for event in repository.audit_events)
    assert repository.audit_events[0]["payload"]["symbol"] == "AMD"
    assert repository.audit_events[1]["payload"]["is_tradable"] is False
    assert repository.audit_events[2]["payload"]["is_active"] is False


def test_live_readiness_api_passes_authenticated_actor(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def __init__(self) -> None:
            self.audit_events: list[dict] = []

        def store_audit_log(self, **kwargs):
            self.audit_events.append(kwargs)

    class FakeService:
        def __init__(self) -> None:
            self.actor: str | None = None
            self.repository = FakeRepository()

        def bootstrap(self) -> dict:
            return {}

        def generate_live_readiness_report(self, *, actor: str = "system"):
            self.actor = actor
            return SimpleNamespace(
                overall_status="BLOCKED",
                live_allowed=False,
                report_id="report-1",
                blockers=1,
                warnings=0,
                reason="blocked",
                version="live_readiness_v1",
            )

    service = FakeService()

    def fake_runtime():
        return FakeSession(), service

    monkeypatch.setattr(common_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_trader_or_admin] = lambda: AdminPrincipal(
        username="readiness-operator",
        role="TRADER",
    )
    try:
        response = client.post("/live-readiness/report")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert service.actor == "readiness-operator"
    assert response.json()["report_id"] == "report-1"
    assert service.repository.audit_events[0]["event_type"] == "MANUAL_OPERATION_RUN"
    assert service.repository.audit_events[0]["actor"] == "readiness-operator"
    assert service.repository.audit_events[0]["entity_id"] == "generate_live_readiness_report"


def test_manual_operation_endpoint_records_operator_audit(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def __init__(self) -> None:
            self.audit_events: list[dict] = []

        def store_audit_log(self, **kwargs):
            self.audit_events.append(kwargs)

    class FakeService:
        def __init__(self, repository: FakeRepository) -> None:
            self.repository = repository

        def run_provider_health(self):
            return SimpleNamespace(
                success=True,
                reason="providers checked",
                version="provider_health_v1",
            )

    repository = FakeRepository()

    def fake_runtime():
        return FakeSession(), FakeService(repository)

    monkeypatch.setattr(admin_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_trader_or_admin] = lambda: AdminPrincipal(
        username="ops-trader",
        role="TRADER",
    )
    try:
        response = client.post("/provider-health/run")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert repository.audit_events == [
        {
            "actor": "ops-trader",
            "event_type": "MANUAL_OPERATION_RUN",
            "entity_type": "manual_operation",
            "entity_id": "provider_health_run",
            "reason": "providers checked",
            "payload": {
                "operation": "provider_health_run",
                "request": {},
                "result": {
                    "success": True,
                    "reason": "providers checked",
                    "version": "provider_health_v1",
                    "result_type": "SimpleNamespace",
                },
            },
        }
    ]


def test_strategies_endpoint_returns_database_approval_state(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def list_rows(self, model: type, limit: int = 100) -> list[dict]:
            assert model.__name__ == "StrategyRegistry"
            return [
                {
                    "strategy_id": "VWAP_RECLAIM",
                    "version": "v1",
                    "status": "APPROVED_FULL_SIZE",
                    "name": "VWAP Reclaim",
                },
                {
                    "strategy_id": "NEWS_MOMENTUM",
                    "version": "v1",
                    "status": "RESEARCH",
                    "name": "News Momentum",
                },
            ][:limit]

    class FakeService:
        def __init__(self) -> None:
            self.repository = FakeRepository()

        def bootstrap(self) -> dict:
            return {}

    def fake_runtime():
        return FakeSession(), FakeService()

    monkeypatch.setattr(admin_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_principal] = lambda: AdminPrincipal(username="viewer", role="VIEWER")
    try:
        response = client.get("/strategies")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    rows = response.json()["strategies"]
    assert rows[0]["status"] == "APPROVED_FULL_SIZE"
    assert rows[0]["paper_trade_allowed"] is True
    assert rows[0]["live_trade_allowed"] is True
    assert rows[1]["status"] == "RESEARCH"
    assert rows[1]["paper_trade_allowed"] is False
    assert rows[1]["live_trade_allowed"] is False


def test_admin_user_management_hashes_redacts_and_audits(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeRepository:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id="user-1",
                username="viewer",
                role="VIEWER",
                is_active=True,
                failed_login_count=2,
                locked_until="locked",
                last_login_at=None,
                reason="created",
                created_at=None,
                updated_at=None,
                source_timestamp=None,
            )
            self.password_hash = ""
            self.audit_events: list[str] = []
            self.revoked_sessions: list[str] = []

        def list_admin_users(self, limit: int = 100) -> list[dict]:
            return [
                {
                    "id": self.user.id,
                    "username": self.user.username,
                    "role": self.user.role,
                    "is_active": self.user.is_active,
                    "failed_login_count": self.user.failed_login_count,
                    "locked_until": self.user.locked_until,
                    "last_login_at": self.user.last_login_at,
                    "reason": self.user.reason,
                    "created_at": self.user.created_at,
                    "updated_at": self.user.updated_at,
                    "source_timestamp": self.user.source_timestamp,
                }
            ][:limit]

        def upsert_admin_user(self, *, username: str, password_hash: str, role: str, reason: str):
            self.user.username = username
            self.user.role = role
            self.user.reason = reason
            self.password_hash = password_hash
            return self.user

        def revoke_admin_sessions_for_user(self, *, user_id: str, reason: str) -> int:
            self.revoked_sessions.append(user_id)
            return 3

        def set_admin_user_role(self, *, username: str, role: str, reason: str):
            self.user.role = role
            self.user.reason = reason
            return self.user

        def clear_admin_user_lockout(self, *, username: str, reason: str):
            self.user.failed_login_count = 0
            self.user.locked_until = None
            self.user.reason = reason
            return self.user

        def store_audit_log(self, **kwargs):
            self.audit_events.append(kwargs)

    class FakeService:
        def __init__(self, repository: FakeRepository) -> None:
            self.repository = repository

        def bootstrap(self) -> dict:
            return {}

    repository = FakeRepository()

    def fake_runtime():
        return FakeSession(), FakeService(repository)

    monkeypatch.setattr(admin_router, "_runtime", fake_runtime)
    app.dependency_overrides[require_admin_token] = lambda: AdminPrincipal(username="admin", role="ADMIN")
    try:
        created = client.post(
            "/admin/users",
            json={
                "username": "viewer",
                "password": "very-secure-password",
                "role": "VIEWER",
                "reason": "Create viewer for monitoring.",
            },
        )
        listed = client.get("/admin/users")
        role = client.post(
            "/admin/users/role",
            json={"username": "viewer", "role": "TRADER", "reason": "Promote for paper ops."},
        )
        unlocked = client.post(
            "/admin/users/unlock",
            json={"username": "viewer", "reason": "Operator verified identity."},
        )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 200
    assert repository.password_hash.startswith(("$2a$", "$2b$", "$2y$"))
    assert "password_hash" not in created.json()["admin_user"]
    assert "password_hash" not in listed.json()["admin_users"][0]
    assert role.json()["admin_user"]["role"] == "TRADER"
    assert unlocked.json()["admin_user"]["failed_login_count"] == 0
    assert unlocked.json()["admin_user"]["locked_until"] is None
    assert repository.revoked_sessions == ["user-1"]
    assert [event["event_type"] for event in repository.audit_events] == [
        "ADMIN_USER_UPSERTED",
        "ADMIN_USER_ROLE_CHANGED",
        "ADMIN_USER_UNLOCKED",
    ]
    assert repository.audit_events[0]["payload"]["revoked_sessions"] == 3


def test_direct_vwap_scanner_and_risk_endpoints_persist_decisions(monkeypatch):
    class FakeSession:
        def close(self):
            pass

    class FakeRepository:
        def __init__(self):
            self.scanner_payloads = []
            self.signals = []
            self.risk_payloads = []
            self.orders = []

        def store_generic_scanner_result(self, **kwargs):
            self.scanner_payloads.append(kwargs)
            return SimpleNamespace(id=f"scanner-{len(self.scanner_payloads)}")

        def store_signal(self, signal):
            self.signals.append(signal)
            return SimpleNamespace(id=f"signal-{len(self.signals)}")

        def store_risk_check(self, risk, **kwargs):
            self.risk_payloads.append({"risk": risk, **kwargs})
            return SimpleNamespace(id=f"risk-{len(self.risk_payloads)}")

        def store_order(self, order, **kwargs):
            self.orders.append({"order": order, **kwargs})
            return SimpleNamespace(id=f"order-{len(self.orders)}")

    repository = FakeRepository()

    def fake_runtime():
        return FakeSession(), SimpleNamespace(repository=repository)

    app.dependency_overrides[require_trader_or_admin] = lambda: AdminPrincipal(
        username="trader",
        role="TRADER",
    )
    monkeypatch.setattr(market_router, "_runtime", fake_runtime)
    monkeypatch.setattr(execution_router, "_runtime", fake_runtime)
    scan = {
        "symbol": "AMD",
        "timestamp": datetime(2026, 6, 3, 14, 31, tzinfo=UTC).isoformat(),
        "price": 101.0,
        "previous_price": 99.0,
        "vwap": 100.0,
        "previous_vwap": 100.0,
        "relative_volume": 2.0,
        "average_volume": 2_000_000,
        "dollar_volume": 100_000_000,
        "spread_bps": 5.0,
        "market_regime": "CHOPPY",
        "strong_relative_strength": True,
    }
    risk = {
        "weekly_loss_pct": 0.0,
        "sector_exposure_pct": 0.0,
        "trades_by_strategy_today": {},
    }

    try:
        scan_response = client.post("/scanners/vwap-reclaim", json=scan)
        risk_response = client.post(
            "/risk/check-vwap-reclaim",
            json={"scan": scan, "risk": risk},
        )
        order_response = client.post(
            "/execution/paper/submit-vwap-reclaim",
            json={"scan": scan, "risk": risk},
        )
    finally:
        app.dependency_overrides.clear()

    assert scan_response.status_code == 200
    assert risk_response.status_code == 200
    assert order_response.status_code == 200
    assert scan_response.json()["scanner_result_id"] == "scanner-1"
    assert scan_response.json()["signal_id"] == "signal-1"
    assert risk_response.json()["scanner_result_id"] == "scanner-2"
    assert risk_response.json()["signal_id"] == "signal-2"
    assert risk_response.json()["risk_check_id"] == "risk-1"
    assert order_response.json()["scanner_result_id"] == "scanner-3"
    assert order_response.json()["signal_id"] == "signal-3"
    assert order_response.json()["risk_check_id"] == "risk-2"
    assert order_response.json()["order_id"] == "order-1"
    assert repository.scanner_payloads[0]["scanner_name"] == "VWAP_RECLAIM_DIRECT_API"
    assert repository.scanner_payloads[0]["accepted"] is True
    assert repository.risk_payloads[0]["risk"].approved is False
    assert repository.risk_payloads[0]["payload"]["source"] == "api:/risk/check-vwap-reclaim"
    assert repository.risk_payloads[1]["payload"]["source"] == "api:/execution/paper/submit-vwap-reclaim"
    assert repository.orders[0]["signal_id"] == "signal-3"


def test_admin_user_endpoint_blocks_self_deactivation_and_demotion():
    app.dependency_overrides[require_admin_token] = lambda: AdminPrincipal(username="admin", role="ADMIN")
    try:
        deactivate = client.post(
            "/admin/users/active",
            json={"username": "admin", "is_active": False, "reason": "test"},
        )
        role = client.post(
            "/admin/users/role",
            json={"username": "admin", "role": "VIEWER", "reason": "test"},
        )
        upsert = client.post(
            "/admin/users",
            json={
                "username": "admin",
                "password": "very-secure-password",
                "role": "VIEWER",
                "reason": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert deactivate.status_code == 409
    assert role.status_code == 409
    assert upsert.status_code == 409


def test_recent_rankings_endpoint_returns_ranked_rows(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeService:
        def __init__(self) -> None:
            self.repository = object()

        def bootstrap(self) -> dict:
            return {}

    captured: dict = {}

    class FakeRankingService:
        def __init__(self, repository, settings) -> None:
            captured["repository"] = repository
            captured["settings"] = settings

        def rank_recent_accepted(self, limit: int):
            captured["limit"] = limit
            return [
                OpportunityRankingResult(
                    scanner_result_id="scan-1",
                    symbol="AMD",
                    strategy_id="VWAP_RECLAIM",
                    scanner_name="VWAP_RECLAIM",
                    opportunity_score=88.5,
                    grade=OpportunityGrade.A_PLUS,
                    reasons=["Strong scanner score.", "Healthy provider."],
                    blocked_reason=None,
                ),
                OpportunityRankingResult(
                    scanner_result_id="scan-2",
                    symbol="MSFT",
                    strategy_id="OPENING_RANGE_BREAKOUT",
                    scanner_name="OPENING_RANGE_BREAKOUT",
                    opportunity_score=0.0,
                    grade=OpportunityGrade.REJECT,
                    reasons=[],
                    blocked_reason="Market data is stale for scanner timeframe.",
                ),
            ]

    class FakeExpectancyView:
        def match(self, *, strategy_id, symbol, regime):
            captured.setdefault("matches", []).append(
                {"strategy_id": strategy_id, "symbol": symbol, "regime": regime}
            )
            return empty_stats(matched_on="overall")

    class FakeExpectancyService:
        def __init__(self, repository) -> None:
            captured["expectancy_repository"] = repository

        def load(self, *, start=None, end=None) -> FakeExpectancyView:
            return FakeExpectancyView()

    def fake_runtime():
        return FakeSession(), FakeService()

    monkeypatch.setattr(market_router, "_runtime", fake_runtime)
    monkeypatch.setattr(market_router, "OpportunityRankingService", FakeRankingService)
    monkeypatch.setattr(market_router, "ExpectancyService", FakeExpectancyService)
    monkeypatch.setattr(market_router, "latest_market_regime", lambda repository: "TRENDING")
    app.dependency_overrides[require_principal] = lambda: AdminPrincipal(username="viewer", role="VIEWER")
    try:
        response = client.get("/rankings/recent", params={"limit": 25})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["limit"] == 25
    rankings = response.json()["rankings"]
    assert [row["symbol"] for row in rankings] == ["AMD", "MSFT"]
    assert rankings[0]["grade"] == "A_PLUS"
    assert rankings[0]["opportunity_score"] == 88.5
    assert rankings[0]["blocked_reason"] is None
    assert rankings[1]["grade"] == "REJECT"
    assert rankings[1]["blocked_reason"] == "Market data is stale for scanner timeframe."
    assert rankings[0]["expectancy"]["sample_size"] == 0
    assert rankings[0]["expectancy"]["version"] == "expectancy_v1"
    assert captured["matches"][0] == {
        "strategy_id": "VWAP_RECLAIM",
        "symbol": "AMD",
        "regime": "TRENDING",
    }


def test_expectancy_summary_endpoint_returns_real_stats(monkeypatch):
    class FakeSession:
        def close(self) -> None:
            pass

    class FakeService:
        def __init__(self) -> None:
            self.repository = object()

        def bootstrap(self) -> dict:
            return {}

    captured: dict = {}

    class FakeView:
        def summary(self) -> dict:
            return {
                "overall": ExpectancyStats(
                    sample_size=3,
                    r_sample_size=2,
                    win_rate=0.6667,
                    avg_r=1.25,
                    median_r=1.1,
                    max_drawdown=-0.8,
                    drawdown_basis="R",
                    avg_time_to_target_seconds=4200.0,
                    failure_rate_before_1030=0.3333,
                    expectancy=42.0,
                    matched_on="overall",
                ),
                "by_symbol": {},
                "by_sector": {},
                "by_regime": {
                    "TRENDING": empty_stats(matched_on="TRENDING"),
                },
            }

    class FakeExpectancyService:
        def __init__(self, repository) -> None:
            captured["repository"] = repository

        def load(self, *, start=None, end=None) -> FakeView:
            captured["start"] = start
            captured["end"] = end
            return FakeView()

    def fake_runtime():
        return FakeSession(), FakeService()

    monkeypatch.setattr(market_router, "_runtime", fake_runtime)
    monkeypatch.setattr(market_router, "ExpectancyService", FakeExpectancyService)
    app.dependency_overrides[require_principal] = lambda: AdminPrincipal(username="viewer", role="VIEWER")
    try:
        response = client.get("/expectancy/summary")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"]["sample_size"] == 3
    assert payload["overall"]["avg_r"] == 1.25
    assert payload["overall"]["drawdown_basis"] == "R"
    assert payload["overall"]["version"] == "expectancy_v1"
    assert payload["by_regime"]["TRENDING"]["sample_size"] == 0
    assert payload["by_regime"]["TRENDING"]["win_rate"] is None


def test_recent_rankings_endpoint_rejects_out_of_range_limit():
    app.dependency_overrides[require_principal] = lambda: AdminPrincipal(username="viewer", role="VIEWER")
    try:
        too_large = client.get("/rankings/recent", params={"limit": 5000})
        too_small = client.get("/rankings/recent", params={"limit": 0})
    finally:
        app.dependency_overrides.clear()

    assert too_large.status_code == 422
    assert too_small.status_code == 422
