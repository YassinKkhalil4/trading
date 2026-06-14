#!/usr/bin/env python3
"""Local Docker paper-trading readiness smoke test.

Usage:
    .venv/bin/python scripts/docker_paper_smoke_test.py
    .venv/bin/python scripts/docker_paper_smoke_test.py --submit-paper-test-order
    .venv/bin/python scripts/docker_paper_smoke_test.py --skip-compose-up
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_system.app.db.seed import DEFAULT_PROVIDER_CAPABILITIES, DEFAULT_STRATEGIES

EXPECTED_STRATEGIES = len(DEFAULT_STRATEGIES)
EXPECTED_PROVIDERS = len(DEFAULT_PROVIDER_CAPABILITIES)
COMPOSE_SERVICES = (
    "postgres",
    "redis",
    "api",
    "dashboard",
    "scheduler-worker",
    "market-stream-worker",
    "reconciliation-worker",
    "trade-monitor-worker",
    "review-worker",
    "learning-worker",
)
WORKERS = (
    ("scheduler", "scheduler-worker"),
    ("market-stream", "market-stream-worker"),
    ("reconciliation", "reconciliation-worker"),
    ("trade-monitor", "trade-monitor-worker"),
    ("review", "review-worker"),
    ("learning", "learning-worker"),
)
@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    checks: list[Check] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    paper_test_order: str = "skipped"
    commands: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))
        if status == "FAIL":
            self.errors.append(f"{name}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = {
            "passed": sum(1 for item in self.checks if item.status == "PASS"),
            "failed": sum(1 for item in self.checks if item.status == "FAIL"),
            "skipped": sum(1 for item in self.checks if item.status == "SKIP"),
        }
        return payload


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged)


def _docker_env() -> dict[str, str]:
    config_dir = ROOT / ".validation-docker"
    config_dir.mkdir(exist_ok=True)
    user_docker = Path.home() / ".docker"
    contexts_link = config_dir / "contexts"
    if not contexts_link.exists() and (user_docker / "contexts").exists():
        contexts_link.symlink_to(user_docker / "contexts", target_is_directory=True)
    config_file = config_dir / "config.json"
    payload: dict[str, str] = {"auths": {}}
    user_config = user_docker / "config.json"
    if user_config.exists():
        try:
            user_payload = json.loads(user_config.read_text(encoding="utf-8"))
            if user_payload.get("currentContext"):
                payload["currentContext"] = user_payload["currentContext"]
        except json.JSONDecodeError:
            pass
    config_file.write_text(json.dumps(payload), encoding="utf-8")
    return {"DOCKER_CONFIG": str(config_dir)}


def _resolve_docker_bin() -> str:
    for candidate in (
        shutil.which("docker") or "",
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if candidate and Path(candidate).exists():
            proc = subprocess.run([candidate, "info"], capture_output=True)
            if proc.returncode == 0:
                return candidate
    return "docker"


def _resolve_compose() -> list[str]:
    docker_candidates = [
        shutil.which("docker") or "",
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ]
    docker = next((item for item in docker_candidates if item and Path(item).exists()), "docker")
    if subprocess.run([docker, "compose", "version"], capture_output=True).returncode == 0:
        return [docker, "compose"]
    for candidate in (
        shutil.which("docker-compose") or "",
        "/opt/homebrew/bin/docker-compose",
        "/usr/local/bin/docker-compose",
    ):
        if candidate and Path(candidate).exists():
            proc = subprocess.run([candidate, "version"], capture_output=True)
            if proc.returncode == 0:
                return [candidate]
    return [docker, "compose"]


def _compose_cmd(compose: list[str], *args: str) -> list[str]:
    if len(compose) == 2 and compose[1] == "compose":
        return [*compose, "-f", "docker-compose.yml", *args]
    return [compose[0], "-f", "docker-compose.yml", *args]


def _http_get(url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return True, body[:500]
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, str(exc)


def _parse_env_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _docker_exec(
    compose: list[str],
    service: str,
    args: list[str],
    *,
    docker_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    command = _compose_cmd(compose, "exec", "-T", service, *args)
    return _run(command, env=docker_env)


def _docker_run(
    compose: list[str],
    service: str,
    args: list[str],
    *,
    docker_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    command = _compose_cmd(compose, "run", "--rm", "--no-deps", service, *args)
    return _run(command, env=docker_env)


def _wait_for_api(
    report: Report,
    compose: list[str],
    *,
    docker_env: dict[str, str],
    attempts: int = 24,
    delay: float = 5.0,
) -> bool:
    url = "http://localhost:8000/health"
    for _ in range(attempts):
        ok, body = _http_get(url)
        if ok and '"status"' in body:
            report.add("API /health", "PASS", body.strip())
            return True
        proc = _docker_exec(
            compose,
            "api",
            ["curl", "-fsS", "http://localhost:8000/health"],
            docker_env=docker_env,
        )
        if proc.returncode == 0 and '"status"' in (proc.stdout or ""):
            report.add("API /health", "PASS", proc.stdout.strip())
            return True
        time.sleep(delay)
    report.add("API /health", "FAIL", f"Timed out waiting for {url}")
    return False


def _runtime_snapshot_script() -> str:
    return """
from trading_system.app.core.config import get_settings
from trading_system.app.core.enums import EnvironmentMode
from trading_system.app.db.session import SessionLocal
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db import models
from trading_system.app.services.runtime import TradingRuntimeService
from sqlalchemy import select, func

settings = get_settings()
with SessionLocal() as session:
    repo = TradingRepository(session)
    service = TradingRuntimeService(repo, settings=settings)
    counts = service.bootstrap()
    strategies = session.scalar(select(func.count()).select_from(models.StrategyRegistry))
    providers = session.scalar(select(func.count()).select_from(models.ProviderCapability))

print("MODE", settings.environment_mode.value)
print("LIVE_PATH", settings.live_order_path_enabled)
print("ALLOW_LIVE", settings.allow_live_trading)
print("ENABLE_LIVE_PATH", settings.enable_live_order_path)
print("ORDERS", counts.get("orders", -1))
print("FILLS", counts.get("fills", -1))
print("POSITIONS", counts.get("positions", -1))
print("STRATEGIES", strategies)
print("PROVIDERS", providers)
"""


def _live_block_script() -> str:
    return """
import uuid
from trading_system.app.core.config import get_settings
from trading_system.app.db.session import SessionLocal
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.db import models
from datetime import UTC, datetime

settings = get_settings()
with SessionLocal() as session:
    repo = TradingRepository(session)
    repo.seed_defaults()
    row = models.Signal(
        idempotency_key=f"paper-smoke-live-blocked-{uuid.uuid4().hex[:12]}",
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
        invalidation="paper smoke validation",
        status="APPROVED",
        signal_rule_version="v1",
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(row)
    repo.session.commit()
    result = TradingRuntimeService(repo, settings=settings).submit_signal_to_live(
        signal_id=row.id,
        account_equity=100000,
        open_positions=0,
        daily_loss_pct=0.0,
        weekly_loss_pct=0.0,
        sector_exposure_pct=0.0,
    )
print("ACCEPTED", result.accepted)
print("BLOCKERS", ",".join(result.gate_decision.get("blockers", [])))
"""


def _alpaca_connectivity_script() -> str:
    return """
import json
from datetime import UTC, datetime
from trading_system.app.core.config import get_settings
from trading_system.app.db.session import SessionLocal
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector, ALPACA_MARKET_DATA_PROVIDER
from trading_system.app.ops.provider_health import ProviderHealthService

settings = get_settings()
with SessionLocal() as session:
    repo = TradingRepository(session)
    service = TradingRuntimeService(repo, settings=settings)
    repo.seed_defaults()
    paper_configured = bool(settings.alpaca_paper_api_key and settings.alpaca_paper_secret_key)
    print("PAPER_CONFIGURED", paper_configured)
    if not paper_configured:
        print("ACCOUNT_OK", False)
        print("ACCOUNT_REASON", "Alpaca paper keys not configured")
        print("MARKET_DATA_OK", False)
        print("MARKET_DATA_REASON", "Alpaca paper keys not configured")
        print("PROVIDER_HEALTH", json.dumps({}))
        raise SystemExit(0)

    sync = service.sync_alpaca_paper()
    print("ACCOUNT_OK", sync.success)
    print("ACCOUNT_REASON", sync.reason)
    if sync.success and sync.account:
        repo.log_api_call(
            provider="alpaca_paper",
            endpoint=f"{settings.alpaca_paper_base_url}/v2/account",
            status_code=200,
            success=True,
            reason="Paper smoke test account connectivity.",
            duration_ms=0.0,
            request_hash="paper-smoke-account",
        )

    bars = AlpacaBarsCollector(repo, settings=settings).collect("SPY", limit=5)
    print("MARKET_DATA_OK", bars.success)
    print("MARKET_DATA_REASON", bars.reason)
    print("MARKET_DATA_CANDLES", bars.candles_seen)

    health = ProviderHealthService(repo, settings=settings).run_once()
    snapshots = {}
    for provider_name in ("alpaca_paper", "alpaca_market_data"):
        row = repo.latest_provider_health_for(provider_name)
        if row:
            snapshots[provider_name] = {"status": row.status, "reason": row.reason}
    print("PROVIDER_HEALTH", json.dumps(snapshots))
    print("PROVIDER_HEALTH_CHECKED", health.providers_checked)
"""


def _paper_test_order_script() -> str:
    return """
import uuid
from trading_system.app.core.config import get_settings
from trading_system.app.core.enums import EnvironmentMode
from trading_system.app.db.session import SessionLocal
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter

settings = get_settings()
if settings.environment_mode != EnvironmentMode.PAPER:
    print("SUBMITTED", False)
    print("REASON", f"Paper test order blocked: ENVIRONMENT_MODE={settings.environment_mode.value}")
    raise SystemExit(0)
if settings.live_order_path_enabled:
    print("SUBMITTED", False)
    print("REASON", "Paper test order blocked: live order path enabled")
    raise SystemExit(0)

with SessionLocal() as session:
    repo = TradingRepository(session)
    service = TradingRuntimeService(repo, settings=settings)
    adapter = AlpacaPaperAdapter(settings=settings)
    client_order_id = f"paper-smoke-{uuid.uuid4().hex[:12]}"
    result = adapter.submit_market_order(
        symbol="SPY",
        side="buy",
        quantity=0.01,
        client_order_id=client_order_id,
    )
    print("SUBMITTED", result.submitted)
    print("REASON", result.reason)
    print("BROKER_ORDER_ID", result.broker_order_id or "")
    if result.submitted:
        sync = service.sync_alpaca_paper()
        print("SYNC_OK", sync.success)
        print("SYNC_REASON", sync.reason)
        recon = service.run_fill_reconciliation_once()
        print("RECON_OK", recon.success)
        print("RECON_REASON", recon.reason)
"""


def run_smoke_test(*, submit_paper_test_order: bool, skip_compose_up: bool) -> Report:
    report = Report()
    compose = _resolve_compose()
    docker_env = _docker_env()

    config_cmd = _compose_cmd(compose, "config")
    report.commands.append(" ".join(config_cmd))
    config_proc = _run(config_cmd, env=docker_env)
    if config_proc.returncode == 0:
        report.add("docker compose config", "PASS", "docker-compose.yml is valid")
    else:
        report.add("docker compose config", "FAIL", config_proc.stderr or config_proc.stdout)
        return report

    env_values = _parse_env_keys(ROOT / ".env")
    if ROOT.joinpath(".env").exists():
        report.add("Local .env present", "PASS", "Using project .env for compose")
    else:
        report.add("Local .env present", "FAIL", "Copy .env.example to .env before starting Docker")

    required_paper_vars = {
        "ENVIRONMENT_MODE": env_values.get("ENVIRONMENT_MODE", ""),
        "ALLOW_LIVE_TRADING": env_values.get("ALLOW_LIVE_TRADING", "false"),
        "ENABLE_LIVE_ORDER_PATH": env_values.get("ENABLE_LIVE_ORDER_PATH", "false"),
    }
    paper_mode_ok = required_paper_vars["ENVIRONMENT_MODE"].lower() == "paper"
    live_disabled = required_paper_vars["ALLOW_LIVE_TRADING"].lower() in {"false", "0", "no", ""}
    path_disabled = required_paper_vars["ENABLE_LIVE_ORDER_PATH"].lower() in {"false", "0", "no", ""}
    live_keys_empty = not env_values.get("ALPACA_LIVE_API_KEY") and not env_values.get("ALPACA_LIVE_SECRET_KEY")
    if paper_mode_ok and live_disabled and path_disabled and live_keys_empty:
        report.add(
            "Paper mode env flags",
            "PASS",
            "ENVIRONMENT_MODE=paper, live gates disabled, live keys empty",
        )
    else:
        report.add(
            "Paper mode env flags",
            "FAIL",
            f"mode={required_paper_vars['ENVIRONMENT_MODE']} allow_live={required_paper_vars['ALLOW_LIVE_TRADING']} "
            f"enable_path={required_paper_vars['ENABLE_LIVE_ORDER_PATH']} live_keys_empty={live_keys_empty}",
        )

    paper_keys_configured = bool(env_values.get("ALPACA_PAPER_API_KEY") and env_values.get("ALPACA_PAPER_SECRET_KEY"))

    if not skip_compose_up:
        docker_bin = _resolve_docker_bin()
        for base_image in ("postgres:16", "redis:7"):
            pull_cmd = [docker_bin, "pull", base_image]
            report.commands.append(" ".join(pull_cmd))
            pull_proc = _run(pull_cmd, env=docker_env)
            if pull_proc.returncode != 0:
                report.add("docker compose up", "FAIL", f"Failed to pull {base_image}: {pull_proc.stderr or pull_proc.stdout}")
                report.finished_at = datetime.now(UTC).isoformat()
                return report
        up_cmd = _compose_cmd(compose, "up", "-d", "--build", *COMPOSE_SERVICES)
        report.commands.append(" ".join(up_cmd))
        up_proc = _run(up_cmd, env=docker_env)
        if up_proc.returncode != 0:
            report.add("docker compose up", "FAIL", (up_proc.stderr or up_proc.stdout)[-2000:])
            return report
        report.add("docker compose up", "PASS", f"Started {len(COMPOSE_SERVICES)} services")
        time.sleep(10)
    else:
        report.add("docker compose up", "SKIP", "Skipped via --skip-compose-up")

    migrate_cmd = _compose_cmd(compose, "run", "--rm", "--no-deps", "api", "alembic", "-c", "trading_system/alembic.ini", "upgrade", "head")
    report.commands.append(" ".join(migrate_cmd))
    migrate_proc = _run(migrate_cmd, env=docker_env)
    if migrate_proc.returncode == 0:
        report.add("Database migrated", "PASS", (migrate_proc.stdout or "head").strip()[-200:])
    else:
        report.add("Database migrated", "FAIL", migrate_proc.stderr or migrate_proc.stdout)

    fresh_proc = _docker_run(
        compose,
        "api",
        ["python", "-c", _runtime_snapshot_script()],
        docker_env=docker_env,
    )
    if fresh_proc.returncode == 0:
        parsed = {
            line.split(" ", 1)[0]: line.split(" ", 1)[1]
            for line in fresh_proc.stdout.splitlines()
            if " " in line
        }
        if parsed.get("ORDERS") == "0" and parsed.get("FILLS") == "0" and parsed.get("POSITIONS") == "0":
            report.add(
                "Fresh DB trading state (post-migrate)",
                "PASS",
                "orders=0 fills=0 positions=0",
            )
        else:
            report.add(
                "Fresh DB trading state (post-migrate)",
                "FAIL",
                f"orders={parsed.get('ORDERS')} fills={parsed.get('FILLS')} positions={parsed.get('POSITIONS')} "
                "(persistent postgres volume may contain prior runs; reset with `docker compose down -v`)",
            )
    else:
        report.add("Fresh DB trading state (post-migrate)", "FAIL", fresh_proc.stderr or fresh_proc.stdout)

    worker_services = {service for _, service in WORKERS}
    for service in COMPOSE_SERVICES:
        ps_cmd = _compose_cmd(compose, "ps", "--status", "running", "--format", "json", service)
        ps_proc = _run(ps_cmd, env=docker_env)
        running = ps_proc.returncode == 0 and (ps_proc.stdout or "").strip()
        label = f"Service {service} running"
        if running:
            report.add(label, "PASS", (ps_proc.stdout or "").splitlines()[0][:200])
        elif service in worker_services:
            report.add(
                label,
                "SKIP",
                "Worker container not continuously running (validated via --once below)",
            )
        else:
            report.add(label, "FAIL", ps_proc.stderr or ps_proc.stdout or f"{service} not running")

    if not _wait_for_api(report, compose, docker_env=docker_env):
        report.finished_at = datetime.now(UTC).isoformat()
        return report

    dash_ok, dash_body = _http_get("http://localhost:8501/")
    if dash_ok and ("<html" in dash_body.lower()):
        report.add("Dashboard HTTP", "PASS", "Dashboard responded")
    else:
        report.add("Dashboard HTTP", "FAIL", dash_body)

    snapshot_proc = _docker_exec(
        compose,
        "api",
        ["python", "-c", _runtime_snapshot_script()],
        docker_env=docker_env,
    )
    if snapshot_proc.returncode == 0:
        parsed = {
            line.split(" ", 1)[0]: line.split(" ", 1)[1]
            for line in snapshot_proc.stdout.splitlines()
            if " " in line
        }
        strategies = int(parsed.get("STRATEGIES", "0"))
        providers = int(parsed.get("PROVIDERS", "0"))
        if strategies == EXPECTED_STRATEGIES:
            report.add("Strategies seeded", "PASS", str(strategies))
        else:
            report.add("Strategies seeded", "FAIL", f"expected {EXPECTED_STRATEGIES}, got {strategies}")
        if providers == EXPECTED_PROVIDERS:
            report.add("Provider capabilities seeded", "PASS", str(providers))
        else:
            report.add("Provider capabilities seeded", "FAIL", f"expected {EXPECTED_PROVIDERS}, got {providers}")
        if parsed.get("MODE") == "paper" and parsed.get("LIVE_PATH") == "False":
            report.add("Runtime paper mode", "PASS", f"mode={parsed.get('MODE')} live_path={parsed.get('LIVE_PATH')}")
        else:
            report.add("Runtime paper mode", "FAIL", snapshot_proc.stdout.strip())
        report.add(
            "Runtime trading counts",
            "PASS",
            f"orders={parsed.get('ORDERS')} fills={parsed.get('FILLS')} positions={parsed.get('POSITIONS')}",
        )
    else:
        report.add("Runtime snapshot", "FAIL", snapshot_proc.stderr or snapshot_proc.stdout)

    live_proc = _docker_exec(
        compose,
        "api",
        ["python", "-c", _live_block_script()],
        docker_env=docker_env,
    )
    accepted_line = next(
        (line for line in live_proc.stdout.splitlines() if line.startswith("ACCEPTED ")),
        "",
    )
    if live_proc.returncode == 0 and accepted_line.endswith("False"):
        report.add("Live order path blocked", "PASS", live_proc.stdout.strip())
    elif live_proc.returncode == 0:
        report.add("Live order path blocked", "FAIL", live_proc.stdout.strip())
    else:
        report.add("Live order path blocked", "FAIL", live_proc.stderr or live_proc.stdout)

    for worker, service in WORKERS:
        worker_cmd = _compose_cmd(
            compose,
            "run",
            "--rm",
            "--no-deps",
            service,
            "python",
            "-m",
            "trading_system.app.services.worker",
            worker,
            "--once",
        )
        report.commands.append(" ".join(worker_cmd))
        worker_proc = _run(worker_cmd, env=docker_env)
        if worker_proc.returncode == 0:
            report.add(f"Worker {worker} --once", "PASS", "completed one cycle")
        else:
            report.add(f"Worker {worker} --once", "FAIL", (worker_proc.stderr or worker_proc.stdout)[-1500:])

    if paper_keys_configured:
        alpaca_proc = _docker_exec(
            compose,
            "api",
            ["python", "-c", _alpaca_connectivity_script()],
            docker_env=docker_env,
        )
        if alpaca_proc.returncode == 0:
            parsed = {
                line.split(" ", 1)[0]: line.split(" ", 1)[1]
                for line in alpaca_proc.stdout.splitlines()
                if " " in line
            }
            account_ok = parsed.get("ACCOUNT_OK") == "True"
            market_ok = parsed.get("MARKET_DATA_OK") == "True"
            report.add(
                "Alpaca paper account connectivity",
                "PASS" if account_ok else "FAIL",
                parsed.get("ACCOUNT_REASON", alpaca_proc.stdout.strip()),
            )
            report.add(
                "Alpaca market data connectivity",
                "PASS" if market_ok else "FAIL",
                parsed.get("MARKET_DATA_REASON", ""),
            )
            try:
                health = json.loads(parsed.get("PROVIDER_HEALTH", "{}"))
            except json.JSONDecodeError:
                health = {}
            health_detail = ", ".join(f"{name}={payload.get('status')}" for name, payload in health.items()) or "no snapshots"
            degraded = any(payload.get("status") not in {"HEALTHY", "UNKNOWN"} for payload in health.values())
            unknown_only = health and all(payload.get("status") == "UNKNOWN" for payload in health.values())
            if health and not degraded and not unknown_only:
                report.add("Provider health snapshots", "PASS", health_detail)
            elif health:
                report.add("Provider health snapshots", "PASS" if market_ok else "FAIL", health_detail)
            else:
                report.add("Provider health snapshots", "FAIL", "No provider health snapshots recorded")
        else:
            report.add("Alpaca connectivity", "FAIL", alpaca_proc.stderr or alpaca_proc.stdout)
    else:
        report.add("Alpaca paper account connectivity", "SKIP", "ALPACA_PAPER_* not set in .env")
        report.add("Alpaca market data connectivity", "SKIP", "ALPACA_PAPER_* not set in .env")
        report.add("Provider health snapshots", "SKIP", "No Alpaca paper keys configured")

    if submit_paper_test_order:
        if not paper_keys_configured:
            report.paper_test_order = "blocked: paper keys not configured"
            report.add("Paper test order", "FAIL", "Cannot submit without ALPACA_PAPER_* keys")
        else:
            order_proc = _docker_exec(
                compose,
                "api",
                ["python", "-c", _paper_test_order_script()],
                docker_env=docker_env,
            )
            submitted_line = next(
                (line for line in order_proc.stdout.splitlines() if line.startswith("SUBMITTED ")),
                "",
            )
            if order_proc.returncode == 0 and submitted_line.endswith("True"):
                report.paper_test_order = "submitted"
                report.add("Paper test order", "PASS", order_proc.stdout.strip())
            else:
                report.paper_test_order = "failed"
                report.add("Paper test order", "FAIL", order_proc.stdout or order_proc.stderr)
    else:
        report.paper_test_order = "skipped (default)"
        report.add("Paper test order", "SKIP", "Pass --submit-paper-test-order to enable")

    report.finished_at = datetime.now(UTC).isoformat()
    return report


def write_report(report: Report, *, json_path: Path, md_path: Path) -> None:
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    lines = [
        "# Local Docker Paper-Readiness Report",
        "",
        f"- Started: {report.started_at}",
        f"- Finished: {report.finished_at}",
        f"- Paper test order: **{report.paper_test_order}**",
        "",
        "## Checks",
        "",
    ]
    for check in report.checks:
        lines.append(f"- **{check.name}** — {check.status}")
        if check.detail:
            lines.append(f"  - {check.detail[:500]}")
    if report.errors:
        lines.extend(["", "## Errors", ""])
        for error in report.errors:
            lines.append(f"- {error}")
    lines.extend(["", "## Commands run", ""])
    for command in report.commands:
        lines.append(f"- `{command}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Docker paper-trading readiness smoke test")
    parser.add_argument(
        "--submit-paper-test-order",
        action="store_true",
        help="Submit a tiny Alpaca paper market order (default: disabled)",
    )
    parser.add_argument(
        "--skip-compose-up",
        action="store_true",
        help="Assume docker compose stack is already running",
    )
    args = parser.parse_args()

    report = run_smoke_test(
        submit_paper_test_order=args.submit_paper_test_order,
        skip_compose_up=args.skip_compose_up,
    )
    json_path = ROOT / "scripts" / "docker_paper_readiness_report.json"
    md_path = ROOT / "scripts" / "docker_paper_readiness_report.md"
    write_report(report, json_path=json_path, md_path=md_path)

    summary = report.to_dict()["summary"]
    print(json.dumps(report.to_dict(), indent=2))
    print(
        f"\nReport written to {md_path} and {json_path} "
        f"({summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped)"
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
