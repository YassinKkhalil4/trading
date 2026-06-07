#!/usr/bin/env python3
"""Real-environment validation for the trading platform.

Runs compile checks, pytest, database migration/seed, environment-mode gates,
worker startup smoke tests, and optional Docker/Terraform checks.

Usage:
    .venv/bin/python scripts/validate_platform.py
    .venv/bin/python scripts/validate_platform.py --skip-pytest
    .venv/bin/python scripts/validate_platform.py --report /tmp/validation_report.md
"""

from __future__ import annotations

import argparse
import compileall
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_VERSION = "1.0.0"
WORKERS = (
    "scheduler",
    "market-stream",
    "reconciliation",
    "trade-monitor",
    "review",
    "learning",
)
EXPECTED_STRATEGIES = 7
EXPECTED_PROVIDERS = len(
    __import__("trading_system.app.db.seed", fromlist=["DEFAULT_PROVIDER_CAPABILITIES"]).DEFAULT_PROVIDER_CAPABILITIES
)


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    command: str = ""


@dataclass
class ValidationReport:
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    passed: list[CheckResult] = field(default_factory=list)
    failed: list[CheckResult] = field(default_factory=list)
    skipped: list[CheckResult] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    aws_next_steps: list[str] = field(default_factory=list)

    def record(self, result: CheckResult) -> None:
        bucket = {"PASS": self.passed, "FAIL": self.failed, "SKIP": self.skipped}[result.status]
        bucket.append(result)
        if result.command:
            self.commands.append(result.command)


def _run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=merged,
        text=True,
        capture_output=True,
    )


def _pass(report: ValidationReport, name: str, detail: str = "", command: str = "") -> None:
    report.record(CheckResult(name, "PASS", detail, command))


def _fail(report: ValidationReport, name: str, detail: str = "", command: str = "") -> None:
    report.record(CheckResult(name, "FAIL", detail, command))


def _skip(report: ValidationReport, name: str, detail: str = "", command: str = "") -> None:
    report.record(CheckResult(name, "SKIP", detail, command))


def check_tooling(report: ValidationReport) -> None:
    for tool in ("docker", "terraform"):
        if shutil.which(tool):
            proc = _run([tool, "--version"])
            _pass(report, f"{tool} available", proc.stdout.splitlines()[0] if proc.stdout else "ok")
        else:
            report.missing_dependencies.append(tool)
            _skip(report, f"{tool} available", f"{tool} not installed on this host")


def check_compileall(report: ValidationReport) -> None:
    command = [sys.executable, "-m", "compileall", "-q", "trading_system"]
    proc = _run(command)
    if proc.returncode == 0:
        _pass(report, "Python compileall", "trading_system package compiles cleanly", " ".join(command))
    else:
        _fail(report, "Python compileall", proc.stderr or proc.stdout, " ".join(command))


def check_pytest(report: ValidationReport, *, skip: bool) -> None:
    if skip:
        _skip(report, "Full pytest suite", "Skipped via --skip-pytest")
        return
    command = [sys.executable, "-m", "pytest", "trading_system/tests", "-q", "--tb=no"]
    proc = _run(command, env={"PYTHONPATH": str(ROOT)})
    summary = (proc.stdout or proc.stderr).strip().splitlines()[-1] if proc.stdout or proc.stderr else "no output"
    if proc.returncode == 0:
        _pass(report, "Full pytest suite", summary, " ".join(command))
    else:
        _fail(report, "Full pytest suite", summary, " ".join(command))


def _postgres_admin_url() -> str | None:
    candidates = [
        os.getenv("VALIDATION_POSTGRES_ADMIN_URL", ""),
        "postgresql+psycopg://trading:trading@localhost:5432/postgres",
        "postgresql+psycopg://postgres:postgres@localhost:5432/postgres",
    ]
    current_user = os.getenv("USER") or os.getenv("USERNAME")
    if current_user:
        candidates.append(f"postgresql+psycopg://{current_user}@localhost:5432/postgres")
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        return None

    from sqlalchemy import create_engine, text

    for url in candidates:
        if not url:
            continue
        try:
            engine = create_engine(url, isolation_level="AUTOCOMMIT")
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return url
        except Exception:
            continue
    return None


def check_alembic_and_seed(report: ValidationReport) -> str | None:
    admin_url = _postgres_admin_url()
    if not admin_url:
        try:
            import psycopg  # noqa: F401
        except ModuleNotFoundError:
            report.missing_dependencies.append("psycopg (pip install 'psycopg[binary]')")
        _skip(
            report,
            "Alembic upgrade head (clean PostgreSQL)",
            "PostgreSQL admin connection unavailable (set VALIDATION_POSTGRES_ADMIN_URL or install psycopg)",
        )
        _skip(report, "Database seed on migrated PostgreSQL", "PostgreSQL unavailable")
        return None

    db_name = f"trading_validation_{int(time.time())}"
    from sqlalchemy import create_engine, text

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    db_url = admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    migrate_cmd = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        "trading_system/alembic.ini",
        "upgrade",
        "head",
    ]
    migrate_env = {
        "DATABASE_URL": db_url,
        "ENVIRONMENT_MODE": "research",
        "DEPLOYMENT_TARGET": "docker",
        "PYTHONPATH": str(ROOT),
    }
    proc = _run(migrate_cmd, env=migrate_env)
    if proc.returncode != 0:
        _fail(
            report,
            "Alembic upgrade head (clean PostgreSQL)",
            (proc.stderr or proc.stdout)[-2000:],
            " ".join(migrate_cmd),
        )
        return db_url

    _pass(report, "Alembic upgrade head (clean PostgreSQL)", f"Database {db_name} migrated to head", " ".join(migrate_cmd))

    seed_env = migrate_env | {
        "ADMIN_USERNAME": "validation_admin",
        "ADMIN_PASSWORD": "validation-password-rotate-me",
        "ADMIN_SESSION_SECRET": "validation-session-secret-not-default",
    }
    seed_script = textwrap.dedent(
        f"""
        import os
        os.environ["DATABASE_URL"] = {db_url!r}
        os.environ["ENVIRONMENT_MODE"] = "research"
        os.environ["DEPLOYMENT_TARGET"] = "docker"
        os.environ["ADMIN_USERNAME"] = "validation_admin"
        os.environ["ADMIN_PASSWORD"] = "validation-password-rotate-me"
        os.environ["ADMIN_SESSION_SECRET"] = "validation-session-secret-not-default"
        from trading_system.app.core.config import get_settings
        get_settings.cache_clear()
        from trading_system.app.db.session import SessionLocal
        from trading_system.app.db.repositories import TradingRepository
        from trading_system.app.security.auth import AuthService
        from trading_system.app.strategies.registry import StrategyRegistryService
        with SessionLocal() as session:
            repo = TradingRepository(session)
            repo.seed_defaults()
            counts = repo.counts()
            strategies = repo.list_rows(__import__("trading_system.app.db.models", fromlist=["StrategyRegistry"]).StrategyRegistry)
            providers = repo.list_rows(__import__("trading_system.app.db.models", fromlist=["ProviderCapability"]).ProviderCapability)
            auth = AuthService(repo, get_settings())
            admin = auth.bootstrap_configured_admin()
            login = auth.login("validation_admin", "validation-password-rotate-me")
            print("COUNTS", counts)
            print("STRATEGIES", len(strategies))
            print("PROVIDERS", len(providers))
            print("ADMIN", admin is not None)
            print("LOGIN", login.authenticated)
            print("SECRET_DEFAULT", get_settings().admin_session_secret == "change-me")
        """
    )
    seed_cmd = [sys.executable, "-c", seed_script]
    seed_proc = _run(seed_cmd, env=seed_env)
    if seed_proc.returncode != 0:
        _fail(report, "Database seed on migrated PostgreSQL", seed_proc.stderr or seed_proc.stdout, "python seed smoke")
        return db_url

    lines = seed_proc.stdout.splitlines()
    parsed = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in lines if " " in line}
    strategies = int(parsed.get("STRATEGIES", "0"))
    providers = int(parsed.get("PROVIDERS", "0"))
    admin_ok = parsed.get("ADMIN") == "True"
    login_ok = parsed.get("LOGIN") == "True"
    secret_default = parsed.get("SECRET_DEFAULT") == "True"

    checks = [
        ("Seed data loads", seed_proc.returncode == 0, parsed.get("COUNTS", "")),
        (f"Strategy registry has {EXPECTED_STRATEGIES} strategies", strategies == EXPECTED_STRATEGIES, str(strategies)),
        (f"Provider capabilities seeded ({EXPECTED_PROVIDERS})", providers == EXPECTED_PROVIDERS, str(providers)),
        ("Admin bootstrap with configured password", admin_ok and login_ok, f"admin={admin_ok} login={login_ok}"),
        ("Admin bootstrap rejects default unsafe session secret", not secret_default, f"default={secret_default}"),
    ]
    for name, ok, detail in checks:
        if ok:
            _pass(report, name, detail)
        else:
            _fail(report, name, detail)

    return db_url


def check_docker(report: ValidationReport) -> None:
    if not shutil.which("docker"):
        for name in (
            "Docker image build (API)",
            "Docker image build (dashboard)",
            "docker-compose config validation",
            "docker-compose startup health check",
        ):
            _skip(report, name, "Docker not installed")
        return

    api_cmd = ["docker", "build", "-t", "trading-platform-api:validation", "."]
    api_proc = _run(api_cmd)
    if api_proc.returncode == 0:
        _pass(report, "Docker image build (API)", "Image trading-platform-api:validation built", " ".join(api_cmd))
    else:
        _fail(report, "Docker image build (API)", (api_proc.stderr or api_proc.stdout)[-2000:], " ".join(api_cmd))

    dash_cmd = [
        "docker",
        "build",
        "-t",
        "trading-platform-dashboard:validation",
        "--build-arg",
        "VALIDATION_TARGET=dashboard",
        ".",
    ]
    dash_proc = _run(dash_cmd)
    if dash_proc.returncode == 0:
        _pass(report, "Docker image build (dashboard)", "Image trading-platform-dashboard:validation built", " ".join(dash_cmd))
    else:
        _fail(
            report,
            "Docker image build (dashboard)",
            "Dashboard uses the same Dockerfile as API; separate tag build attempted. "
            + (dash_proc.stderr or dash_proc.stdout)[-1500:],
            " ".join(dash_cmd),
        )

    compose_cmd = ["docker", "compose", "-f", "docker-compose.yml", "config"]
    compose_proc = _run(compose_cmd)
    if compose_proc.returncode == 0:
        _pass(report, "docker-compose config validation", "docker-compose.yml is valid", " ".join(compose_cmd))
    else:
        _fail(report, "docker-compose config validation", compose_proc.stderr or compose_proc.stdout, " ".join(compose_cmd))

    up_cmd = ["docker", "compose", "up", "-d", "postgres", "redis", "api"]
    up_proc = _run(up_cmd)
    if up_proc.returncode != 0:
        _fail(report, "docker-compose startup health check", up_proc.stderr or up_proc.stdout, " ".join(up_cmd))
        return

    health_cmd = ["docker", "compose", "exec", "-T", "api", "curl", "-fsS", "http://localhost:8000/health"]
    healthy = False
    for _ in range(12):
        health_proc = _run(health_cmd)
        if health_proc.returncode == 0 and '"status"' in (health_proc.stdout or ""):
            healthy = True
            break
        time.sleep(5)
    down_cmd = ["docker", "compose", "down"]
    _run(down_cmd)
    if healthy:
        _pass(report, "docker-compose startup health check", health_proc.stdout.strip(), " ".join(health_cmd))
    else:
        _fail(report, "docker-compose startup health check", health_proc.stderr or health_proc.stdout, " ".join(health_cmd))


def check_environment_modes(report: ValidationReport) -> None:
    from trading_system.app.core.config import Settings, get_settings
    from trading_system.app.core.enums import EnvironmentMode
    from trading_system.app.execution.paper_execution import PaperExecutionEngine
    from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter
    from trading_system.app.execution.reconciliation import ReconciliationResult
    from trading_system.app.risk.risk_engine import RiskDecision
    from trading_system.app.services.runtime import TradingRuntimeService
    from trading_system.app.signals.signal_engine import TradeSignal
    from trading_system.app.core.enums import Direction, TradeType
    from trading_system.app.db.session import build_engine
    from trading_system.app.db.base import Base
    from trading_system.app.db.repositories import TradingRepository
    from sqlalchemy.orm import sessionmaker

    get_settings.cache_clear()

    research_settings = Settings(environment_mode=EnvironmentMode.RESEARCH)
    paper_engine = PaperExecutionEngine(settings=research_settings)
    signal = TradeSignal(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(100.0, 101.0),
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="test",
        source_timestamp=datetime.now(UTC),
        idempotency_key="validation-research",
        rule_version="v1",
    )
    blocked = paper_engine.submit_limit_order(
        signal=signal,
        risk_decision=RiskDecision(True, "ok", "v1", 10, 50.0),
        reconciliation=ReconciliationResult(True, "ok"),
    )
    if blocked.status.value == "REJECTED" and "ENVIRONMENT_MODE=paper" in blocked.reason:
        _pass(report, "research starts without broker execution", blocked.reason)
    else:
        _fail(report, "research starts without broker execution", str(blocked))

    paper_settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="paper-key",
        alpaca_paper_secret_key="paper-secret",
        alpaca_live_api_key="live-should-not-be-used",
        alpaca_live_secret_key="live-should-not-be-used",
    )
    if not paper_settings.live_order_path_enabled:
        _pass(report, "paper starts with Alpaca paper config only", "live_order_path_enabled=False in paper mode")
    else:
        _fail(report, "paper starts with Alpaca paper config only", "live path unexpectedly enabled")
    adapter = AlpacaPaperAdapter(paper_settings)
    if adapter.configured and not AlpacaPaperAdapter(
        Settings(environment_mode=EnvironmentMode.PAPER)
    ).configured:
        _pass(report, "paper adapter requires paper credentials", "configured only when paper keys present")
    else:
        _fail(report, "paper adapter requires paper credentials", "paper credential gating mismatch")

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    repo = TradingRepository(sessionmaker(bind=engine)())
    repo.seed_defaults()
    live_disabled = TradingRuntimeService(repo, settings=Settings(environment_mode=EnvironmentMode.LIVE_DISABLED))
    live_disabled_result = live_disabled.submit_signal_to_live(
        signal_id=_store_minimal_signal(repo),
        account_equity=100_000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
    )
    blockers = live_disabled_result.gate_decision.get("blockers", [])
    if not live_disabled_result.accepted and blockers:
        _pass(report, "live_disabled blocks live order path", ", ".join(blockers))
    else:
        _fail(report, "live_disabled blocks live order path", str(live_disabled_result))

    os.environ["ENVIRONMENT_MODE"] = "live"
    os.environ["ALLOW_LIVE_TRADING"] = "false"
    get_settings.cache_clear()
    try:
        get_settings()
        _fail(report, "live remains blocked without every gate", "get_settings() did not raise")
    except RuntimeError as exc:
        if "Live trading is not wired" in str(exc):
            _pass(report, "live remains blocked without every gate", str(exc))
        else:
            _fail(report, "live remains blocked without every gate", str(exc))
    finally:
        os.environ.pop("ENVIRONMENT_MODE", None)
        os.environ.pop("ALLOW_LIVE_TRADING", None)
        get_settings.cache_clear()


def _store_minimal_signal(repo: Any) -> str:
    from trading_system.app.db import models

    row = models.Signal(
        idempotency_key="validation-live-disabled",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction="LONG",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=95.0,
        target_1=110.0,
        target_2=115.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="test",
        status="APPROVED",
        signal_rule_version="v1",
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(row)
    repo.session.commit()
    return row.id


def check_workers(report: ValidationReport) -> None:
    with tempfile.TemporaryDirectory(prefix="trading-worker-validation-") as tmp:
        db_path = Path(tmp) / "workers.db"
        base_env = {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "ENVIRONMENT_MODE": "research",
            "DEPLOYMENT_TARGET": "local",
            "PYTHONPATH": str(ROOT),
        }
        for worker in WORKERS:
            cmd = [sys.executable, "-m", "trading_system.app.services.worker", worker, "--once"]
            proc = _run(cmd, env=base_env)
            if proc.returncode == 0:
                _pass(report, f"worker {worker} imports and starts", "completed one cycle", " ".join(cmd))
            else:
                _fail(
                    report,
                    f"worker {worker} imports and starts",
                    (proc.stderr or proc.stdout)[-2000:],
                    " ".join(cmd),
                )


def check_no_fake_trading_data(report: ValidationReport) -> None:
    from trading_system.app.core.config import Settings, get_settings
    from trading_system.app.core.enums import EnvironmentMode
    from trading_system.app.db.session import build_engine
    from trading_system.app.db.base import Base
    from trading_system.app.db.repositories import TradingRepository
    from trading_system.app.services.runtime import TradingRuntimeService
    from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter
    from trading_system.app.ops.provider_health import ProviderHealthService
    from sqlalchemy.orm import sessionmaker
    from fastapi.testclient import TestClient

    get_settings.cache_clear()
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    repo = TradingRepository(sessionmaker(bind=engine)())
    service = TradingRuntimeService(
        repo,
        settings=Settings(environment_mode=EnvironmentMode.RESEARCH, admin_session_secret="validation-secret"),
    )
    counts = service.bootstrap()
    snapshot = service.dashboard_snapshot()

    trading_empty = (
        counts.get("orders", 0) == 0
        and counts.get("fills", 0) == 0
        and counts.get("positions", 0) == 0
        and snapshot["counts"]["orders"] == 0
        and snapshot["counts"]["fills"] == 0
        and len(snapshot["positions"]) == 0
    )
    if trading_empty:
        _pass(report, "dashboard/API show no fake positions/orders/fills on fresh bootstrap", str(counts))
    else:
        _fail(report, "dashboard/API show no fake positions/orders/fills on fresh bootstrap", str(counts))

    paper_sync = AlpacaPaperAdapter(Settings(environment_mode=EnvironmentMode.PAPER)).sync()
    if not paper_sync.configured and not paper_sync.success and paper_sync.positions == []:
        _pass(report, "missing paper credentials show unconfigured status", paper_sync.reason)
    else:
        _fail(report, "missing paper credentials show unconfigured status", str(paper_sync))

    health = ProviderHealthService(repo, Settings()).run_once()
    latest = repo.latest_provider_health(20)
    unknown = [row for row in latest if row["status"] in {"UNKNOWN", "STALE", "DEGRADED", "DOWN"}]
    if health.providers_checked >= EXPECTED_PROVIDERS and unknown:
        _pass(report, "missing provider credentials show blocked/unconfigured health", f"{len(unknown)} non-healthy snapshots")
    else:
        _fail(report, "missing provider credentials show blocked/unconfigured health", str(latest[:3]))

    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["ENVIRONMENT_MODE"] = "research"
    get_settings.cache_clear()
    from trading_system.app.api.main import app

    client = TestClient(app)
    health_resp = client.get("/health")
    if health_resp.status_code == 200 and "disabled" in health_resp.json().get("message", "").lower():
        _pass(report, "API /health advertises gated live path", health_resp.json()["message"])
    else:
        _fail(report, "API /health advertises gated live path", health_resp.text)


def build_aws_next_steps(report: ValidationReport) -> None:
    report.aws_next_steps = [
        "Install and configure AWS CLI credentials for the target account/region (default us-east-1).",
        "Review infra/aws/main.tf and set required variables (VPC CIDR, secrets ARNs, image tags).",
        "Run `terraform -chdir=infra/aws init` and `terraform -chdir=infra/aws plan` before any apply.",
        "Build and push the application image to the ECR repository declared in Terraform.",
        "Run the Alembic migration ECS task against the RDS endpoint (see .github/workflows/ci-cd.yml).",
        "Set runtime secrets in AWS Secrets Manager: DATABASE_URL, ADMIN_PASSWORD, ADMIN_SESSION_SECRET, ALPACA_PAPER_*.",
        "Deploy ECS services in order: api, dashboard, scheduler, market_stream, reconciliation, trade_monitor, reviews, learning.",
        "Verify ALB /health on the API service and authenticated /ops/health after deployment.",
        "Confirm ENVIRONMENT_MODE=paper, ALLOW_LIVE_TRADING=false, ENABLE_LIVE_ORDER_PATH=false in task definitions.",
        "Generate a live-readiness report only after all provider health and admin-secret gates pass in the target environment.",
    ]


def render_markdown(report: ValidationReport) -> str:
    def section(title: str, items: list[CheckResult]) -> str:
        if not items:
            return f"## {title}\n\n_None._\n"
        lines = [f"## {title}", ""]
        for item in items:
            lines.append(f"- **{item.name}** — {item.status}")
            if item.detail:
                lines.append(f"  - {item.detail}")
            if item.command:
                lines.append(f"  - Command: `{item.command}`")
        lines.append("")
        return "\n".join(lines)

    lines = [
        "# Platform Validation Report",
        "",
        f"- Report version: {REPORT_VERSION}",
        f"- Started: {report.started_at}",
        f"- Finished: {report.finished_at}",
        f"- Root: `{ROOT}`",
        "",
        section("Passed checks", report.passed),
        section("Failed checks", report.failed),
        section("Skipped checks", report.skipped),
        "## Commands run",
        "",
    ]
    if report.commands:
        lines.extend(f"- `{cmd}`" for cmd in report.commands)
    else:
        lines.append("_None recorded._")
    lines.extend(
        [
            "",
            "## Missing local dependencies",
            "",
        ]
    )
    if report.missing_dependencies:
        lines.extend(f"- {dep}" for dep in report.missing_dependencies)
    else:
        lines.append("_None detected._")
    lines.extend(["", "## Exact next steps for AWS validation", ""])
    lines.extend(f"{idx}. {step}" for idx, step in enumerate(report.aws_next_steps, start=1))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate trading platform real-environment readiness.")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip the full pytest suite.")
    parser.add_argument("--report", type=Path, help="Optional path to write markdown report.")
    args = parser.parse_args()

    report = ValidationReport()
    check_tooling(report)
    check_compileall(report)
    check_pytest(report, skip=args.skip_pytest)
    check_alembic_and_seed(report)
    check_environment_modes(report)
    check_workers(report)
    check_no_fake_trading_data(report)
    check_docker(report)
    build_aws_next_steps(report)

    report.finished_at = datetime.now(UTC).isoformat()
    markdown = render_markdown(report)
    print(markdown)
    if args.report:
        args.report.write_text(markdown, encoding="utf-8")

    summary_path = ROOT / "scripts" / "validation_report.json"
    summary_path.write_text(
        json.dumps(
            {
                "passed": [item.__dict__ for item in report.passed],
                "failed": [item.__dict__ for item in report.failed],
                "skipped": [item.__dict__ for item in report.skipped],
                "commands": report.commands,
                "missing_dependencies": report.missing_dependencies,
                "aws_next_steps": report.aws_next_steps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
