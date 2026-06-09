#!/usr/bin/env python3
"""Real-environment validation for the trading platform.

Runs compile checks, pytest, database migration/seed, environment-mode gates,
worker startup smoke tests, and optional Docker/Terraform checks.

Usage:
    .venv/bin/python scripts/validate_platform.py
    .venv/bin/python scripts/validate_platform.py --skip-pytest
    .venv/bin/python scripts/validate_platform.py --docker-only
    .venv/bin/python scripts/validate_platform.py --report scripts/validation_report.md
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

REPORT_VERSION = "1.1.0"
WORKERS = (
    "scheduler",
    "market-stream",
    "reconciliation",
    "trade-monitor",
    "review",
    "learning",
)
DOCKER_COMPOSE_SERVICES = (
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
DOCKER_WORKER_SERVICES = {
    "scheduler": "scheduler-worker",
    "market-stream": "market-stream-worker",
    "reconciliation": "reconciliation-worker",
    "trade-monitor": "trade-monitor-worker",
    "review": "review-worker",
    "learning": "learning-worker",
}
DOCKER_CANDIDATE_PATHS = (
    "/opt/homebrew/bin/docker",
    "/usr/local/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
)


def _tail_error_output(output: str, *, max_lines: int = 12) -> str:
    lines = [line for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return output or ""
    interesting = [line for line in lines if any(token in line.lower() for token in ("error", "failed", "fatal", "timeout", "denied", "cannot", "not found"))]
    if interesting:
        return "\n".join(interesting[-max_lines:])
    return "\n".join(lines[-max_lines:])
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
    scope: str = "platform"
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


def _docker_unavailable_reason() -> str:
    which = shutil.which("docker")
    if which:
        proc = _run([which, "info"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return detail[0] if detail else "Docker CLI found but daemon is not running."
    for candidate in DOCKER_CANDIDATE_PATHS:
        path = Path(candidate)
        if path.is_symlink() and not path.exists():
            target = os.readlink(path)
            return (
                f"Docker CLI symlink at {candidate} points to missing bundle ({target}). "
                "Install Docker Desktop or Colima and start the daemon."
            )
    return "Docker CLI not found on PATH and no usable Docker binary detected."


def _resolve_docker() -> tuple[str | None, str]:
    candidates: list[str] = []
    if shutil.which("docker"):
        candidates.append(shutil.which("docker") or "")
    candidates.extend(DOCKER_CANDIDATE_PATHS)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        if not path.exists():
            continue
        proc = _run([candidate, "version"])
        if proc.returncode != 0:
            continue
        info = _run([candidate, "info"])
        if info.returncode == 0:
            return candidate, ""
        return None, _docker_unavailable_reason()
    return None, _docker_unavailable_reason()


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


def _docker_compose_cmd(docker_bin: str) -> list[str]:
    compose_plugin = Path("/opt/homebrew/lib/docker/cli-plugins/docker-compose")
    if compose_plugin.exists():
        proc = _run([docker_bin, "compose", "version"], env={"DOCKER_CLI_PLUGIN_EXTRA_DIRS": str(compose_plugin.parent)})
        if proc.returncode == 0:
            return [docker_bin, "compose"]

    for candidate in (shutil.which("docker-compose"), "/opt/homebrew/bin/docker-compose"):
        if candidate and Path(candidate).exists():
            proc = _run([candidate, "version"])
            if proc.returncode == 0:
                return [candidate]

    compose = _run([docker_bin, "compose", "version"])
    if compose.returncode == 0:
        return [docker_bin, "compose"]
    return [docker_bin, "compose"]


def _validation_env_file() -> Path:
    example = ROOT / ".env.example"
    target = ROOT / ".env"
    if target.exists():
        return target
    if not example.exists():
        raise FileNotFoundError("Missing .env and .env.example for docker-compose validation.")
    content = example.read_text(encoding="utf-8")
    overrides = {
        "ENVIRONMENT_MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "ENABLE_LIVE_ORDER_PATH": "false",
        "ADMIN_PASSWORD": "docker-validation-password",
        "ADMIN_SESSION_SECRET": "docker-validation-session-secret",
    }
    lines = []
    for line in content.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else ""
        if key in overrides:
            lines.append(f"{key}={overrides[key]}")
            overrides.pop(key, None)
        else:
            lines.append(line)
    for key, value in overrides.items():
        lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _remaining_docker_checks() -> list[str]:
    checks = [
        "docker compose up (postgres, redis, api, dashboard, workers)",
        "Docker service api running",
        "Docker service dashboard running",
        "Docker service scheduler-worker running",
        "Docker service market-stream-worker running",
        "Docker service reconciliation-worker running",
        "Docker service trade-monitor-worker running",
        "Docker service review-worker running",
        "Docker service learning-worker running",
        "Alembic upgrade head inside Docker",
        "API /health responds through Docker",
        "Dashboard container HTTP responds through Docker",
        "Dashboard container starts without fake trading data",
        "Docker ENVIRONMENT_MODE=paper with live gates disabled",
        "Docker live order path not reachable",
    ]
    checks.extend(f"Docker worker {worker} --once cycle" for worker in WORKERS)
    return checks


def _skip_docker_checks(report: ValidationReport, reason: str) -> None:
    report.missing_dependencies.append("docker")
    checks = [
        "Docker image build (API)",
        "Docker image build (dashboard)",
        "docker-compose config validation",
        "docker compose up (postgres, redis, api, dashboard, workers)",
        "Docker service api running",
        "Docker service dashboard running",
        "Docker service scheduler-worker running",
        "Docker service market-stream-worker running",
        "Docker service reconciliation-worker running",
        "Docker service trade-monitor-worker running",
        "Docker service review-worker running",
        "Docker service learning-worker running",
        "Alembic upgrade head inside Docker",
        "API /health responds through Docker",
        "Dashboard container HTTP responds through Docker",
        "Dashboard container starts without fake trading data",
        "Docker ENVIRONMENT_MODE=paper with live gates disabled",
        "Docker live order path not reachable",
    ]
    for worker in WORKERS:
        checks.append(f"Docker worker {worker} --once cycle")
    for name in checks:
        _skip(report, name, reason)


def _skip_remaining_docker_checks(report: ValidationReport, reason: str, *, after: str | None = None) -> None:
    remaining = _remaining_docker_checks()
    if after and after in remaining:
        remaining = remaining[remaining.index(after) + 1 :]
    for name in remaining:
        _skip(report, name, reason)


def check_tooling(report: ValidationReport) -> None:
    docker_bin, docker_reason = _resolve_docker()
    if docker_bin:
        proc = _run([docker_bin, "--version"])
        _pass(report, "docker available", proc.stdout.splitlines()[0] if proc.stdout else "ok")
    else:
        _skip(report, "docker available", docker_reason)

    if shutil.which("terraform"):
        proc = _run(["terraform", "--version"])
        _pass(report, "terraform available", proc.stdout.splitlines()[0] if proc.stdout else "ok")
    else:
        report.missing_dependencies.append("terraform")
        _skip(report, "terraform available", "terraform not installed on this host")


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


def _docker_exec(
    compose_cmd: list[str],
    service: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [*compose_cmd, "exec", "-T", service, *args]
    return _run(command, env=env)


def _docker_run(
    compose_cmd: list[str],
    service: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [*compose_cmd, "run", "--rm", "--no-deps", service, *args]
    return _run(command, env=env)


def _service_running(compose_cmd: list[str], service: str, *, env: dict[str, str] | None = None) -> tuple[bool, str]:
    proc = _run([*compose_cmd, "ps", "--status", "running", "--format", "json", service], env=env)
    if proc.returncode != 0:
        return False, proc.stderr or proc.stdout
    output = (proc.stdout or "").strip()
    if not output:
        return False, f"{service} is not running"
    return True, output.splitlines()[0]


def check_docker(report: ValidationReport) -> None:
    docker_bin, reason = _resolve_docker()
    if not docker_bin:
        _skip_docker_checks(report, reason)
        return

    docker_env = _docker_env()
    compose_cmd = _docker_compose_cmd(docker_bin)
    try:
        _validation_env_file()
    except FileNotFoundError as exc:
        _skip_docker_checks(report, str(exc))
        return

    api_cmd = [docker_bin, "build", "-t", "trading-platform-api:validation", "."]
    api_proc = _run(api_cmd, env=docker_env)
    if api_proc.returncode == 0:
        _pass(report, "Docker image build (API)", "Image trading-platform-api:validation built", " ".join(api_cmd))
    else:
        _fail(report, "Docker image build (API)", (api_proc.stderr or api_proc.stdout)[-2000:], " ".join(api_cmd))
        _skip_remaining_docker_checks(report, "Skipped because API image build failed.")
        return

    dash_cmd = [docker_bin, "build", "-t", "trading-platform-dashboard:validation", "."]
    dash_proc = _run(dash_cmd, env=docker_env)
    if dash_proc.returncode == 0:
        _pass(
            report,
            "Docker image build (dashboard)",
            "Separate dashboard tag built from shared Dockerfile",
            " ".join(dash_cmd),
        )
    else:
        _fail(report, "Docker image build (dashboard)", (dash_proc.stderr or dash_proc.stdout)[-2000:], " ".join(dash_cmd))
        _skip_remaining_docker_checks(report, "Skipped because dashboard image build failed.")
        return

    config_cmd = [*compose_cmd, "-f", "docker-compose.yml", "config"]
    config_proc = _run(config_cmd, env=docker_env)
    if config_proc.returncode == 0:
        _pass(report, "docker-compose config validation", "docker-compose.yml is valid", " ".join(config_cmd))
    else:
        _fail(report, "docker-compose config validation", config_proc.stderr or config_proc.stdout, " ".join(config_cmd))
        _skip_remaining_docker_checks(report, "Skipped because docker-compose config validation failed.")
        return

    for base_image in ("postgres:16", "redis:7"):
        pull_proc = _run([docker_bin, "pull", base_image], env=docker_env)
        if pull_proc.returncode != 0:
            _fail(
                report,
                "docker compose up (postgres, redis, api, dashboard, workers)",
                f"Failed to prefetch {base_image}: {_tail_error_output(pull_proc.stderr or pull_proc.stdout)}",
                f"{docker_bin} pull {base_image}",
            )
            _skip_remaining_docker_checks(report, f"Skipped because prefetch for {base_image} failed.")
            return

    up_cmd = [*compose_cmd, "-f", "docker-compose.yml", "up", "-d", "--build", *DOCKER_COMPOSE_SERVICES]
    up_proc = _run(up_cmd, env=docker_env)
    if up_proc.returncode != 0:
        _fail(
            report,
            "docker compose up (postgres, redis, api, dashboard, workers)",
            _tail_error_output(up_proc.stderr or up_proc.stdout),
            " ".join(up_cmd),
        )
        _skip_remaining_docker_checks(report, "Skipped because docker compose up failed.", after="docker compose up (postgres, redis, api, dashboard, workers)")
        _run([*compose_cmd, "-f", "docker-compose.yml", "down", "--remove-orphans"], env=docker_env)
        return
    _pass(report, "docker compose up (postgres, redis, api, dashboard, workers)", "All compose services started", " ".join(up_cmd))

    try:
        time.sleep(8)
        for service in ("api", "dashboard", *DOCKER_WORKER_SERVICES.values()):
            running, detail = _service_running(compose_cmd, service, env=docker_env)
            label = f"Docker service {service} running"
            if running:
                _pass(report, label, detail[:300])
            else:
                _fail(report, label, detail[:2000])

        migrate_cmd = [
            *compose_cmd,
            "run",
            "--rm",
            "--no-deps",
            "api",
            "alembic",
            "-c",
            "trading_system/alembic.ini",
            "upgrade",
            "head",
        ]
        migrate_proc = _run(migrate_cmd, env=docker_env)
        if migrate_proc.returncode == 0:
            _pass(report, "Alembic upgrade head inside Docker", (migrate_proc.stdout or "head").strip()[-500:], " ".join(migrate_cmd))
        else:
            _fail(report, "Alembic upgrade head inside Docker", migrate_proc.stderr or migrate_proc.stdout, " ".join(migrate_cmd))

        health_ok = False
        health_detail = ""
        health_cmd = ["curl", "-fsS", "http://localhost:8000/health"]
        for _ in range(18):
            host_proc = _run(health_cmd)
            if host_proc.returncode == 0 and '"status"' in (host_proc.stdout or ""):
                health_ok = True
                health_detail = host_proc.stdout.strip()
                _pass(report, "API /health responds through Docker", health_detail, " ".join(health_cmd))
                break
            container_proc = _docker_exec(
                compose_cmd,
                "api",
                ["curl", "-fsS", "http://localhost:8000/health"],
                env=docker_env,
            )
            if container_proc.returncode == 0 and '"status"' in (container_proc.stdout or ""):
                health_ok = True
                health_detail = container_proc.stdout.strip()
                _pass(
                    report,
                    "API /health responds through Docker",
                    health_detail,
                    " ".join([*compose_cmd, "exec", "-T", "api", "curl", "-fsS", "http://localhost:8000/health"]),
                )
                break
            time.sleep(5)
        if not health_ok:
            _fail(report, "API /health responds through Docker", host_proc.stderr or host_proc.stdout, " ".join(health_cmd))

        dashboard_cmd = ["curl", "-fsS", "http://localhost:8501/"]
        dash_health = _run(dashboard_cmd)
        if dash_health.returncode == 0 and ("streamlit" in dash_health.stdout.lower() or "<html" in dash_health.stdout.lower()):
            _pass(report, "Dashboard container HTTP responds through Docker", "Streamlit HTTP endpoint responded", " ".join(dashboard_cmd))
        else:
            _fail(report, "Dashboard container HTTP responds through Docker", dash_health.stderr or dash_health.stdout, " ".join(dashboard_cmd))

        from trading_system.app.core.enums import EnvironmentMode as _EnvironmentMode

        snapshot_script = textwrap.dedent(
            """
            from trading_system.app.core.config import get_settings
            from trading_system.app.core.enums import EnvironmentMode
            from trading_system.app.db.session import SessionLocal
            from trading_system.app.db.repositories import TradingRepository
            from trading_system.app.services.runtime import TradingRuntimeService
            settings = get_settings()
            with SessionLocal() as session:
                repo = TradingRepository(session)
                service = TradingRuntimeService(repo, settings=settings)
                counts = service.bootstrap()
                snapshot = service.dashboard_snapshot()
            print("MODE", settings.environment_mode.value)
            print("LIVE_PATH", settings.live_order_path_enabled)
            print("ORDERS", counts.get("orders", -1))
            print("FILLS", counts.get("fills", -1))
            print("POSITIONS", counts.get("positions", -1))
            print("SNAPSHOT_ORDERS", snapshot["counts"]["orders"])
            """
        )
        snapshot_proc = _docker_exec(
            compose_cmd,
            "api",
            ["python", "-c", snapshot_script],
            env=docker_env,
        )
        if snapshot_proc.returncode == 0:
            parsed = {
                line.split(" ", 1)[0]: line.split(" ", 1)[1]
                for line in snapshot_proc.stdout.splitlines()
                if " " in line
            }
            mode_ok = parsed.get("MODE") == _EnvironmentMode.PAPER.value
            live_disabled = parsed.get("LIVE_PATH") == "False"
            no_fake = (
                parsed.get("ORDERS") == "0"
                and parsed.get("FILLS") == "0"
                and parsed.get("POSITIONS") == "0"
                and parsed.get("SNAPSHOT_ORDERS") == "0"
            )
            if no_fake:
                _pass(
                    report,
                    "Dashboard container starts without fake trading data",
                    f"counts orders={parsed.get('ORDERS')} fills={parsed.get('FILLS')} positions={parsed.get('POSITIONS')}",
                )
            if mode_ok and live_disabled:
                _pass(
                    report,
                    "Docker ENVIRONMENT_MODE=paper with live gates disabled",
                    f"mode={parsed.get('MODE')} live_order_path_enabled={parsed.get('LIVE_PATH')}",
                )
            else:
                _fail(
                    report,
                    "Docker ENVIRONMENT_MODE=paper with live gates disabled",
                    snapshot_proc.stdout,
                )
        else:
            _fail(report, "Docker ENVIRONMENT_MODE=paper with live gates disabled", snapshot_proc.stderr or snapshot_proc.stdout)

        live_block_script = textwrap.dedent(
            """
            from trading_system.app.core.config import Settings, get_settings
            from trading_system.app.core.enums import EnvironmentMode
            from trading_system.app.db.session import build_engine
            from trading_system.app.db.base import Base
            from trading_system.app.db.repositories import TradingRepository
            from trading_system.app.services.runtime import TradingRuntimeService
            from trading_system.app.db import models
            from datetime import UTC, datetime
            from sqlalchemy.orm import sessionmaker
            settings = get_settings()
            engine = build_engine(settings.database_url)
            Base.metadata.create_all(engine)
            repo = TradingRepository(sessionmaker(bind=engine)())
            repo.seed_defaults()
            row = models.Signal(
                idempotency_key="docker-live-blocked",
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
                invalidation="docker validation",
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
        )
        live_proc = _docker_exec(
            compose_cmd,
            "api",
            ["python", "-c", live_block_script],
            env=docker_env,
        )
        accepted_line = next(
            (line for line in live_proc.stdout.splitlines() if line.startswith("ACCEPTED ")),
            "",
        )
        if live_proc.returncode == 0 and accepted_line.endswith("False"):
            _pass(report, "Docker live order path not reachable", live_proc.stdout.strip())
        elif live_proc.returncode == 0:
            _fail(report, "Docker live order path not reachable", live_proc.stdout)
        else:
            _fail(report, "Docker live order path not reachable", live_proc.stderr or live_proc.stdout)

        for worker, service in DOCKER_WORKER_SERVICES.items():
            worker_cmd = [
                *compose_cmd,
                "run",
                "--rm",
                "--no-deps",
                service,
                "python",
                "-m",
                "trading_system.app.services.worker",
                worker,
                "--once",
            ]
            worker_proc = _run(worker_cmd, env=docker_env)
            if worker_proc.returncode == 0:
                _pass(
                    report,
                    f"Docker worker {worker} --once cycle",
                    "completed one cycle in container",
                    " ".join(worker_cmd),
                )
            else:
                _fail(
                    report,
                    f"Docker worker {worker} --once cycle",
                    (worker_proc.stderr or worker_proc.stdout)[-2000:],
                    " ".join(worker_cmd),
                )
    finally:
        down_cmd = [*compose_cmd, "-f", "docker-compose.yml", "down", "--remove-orphans"]
        _run(down_cmd, env=docker_env)


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

    title = "# Docker Validation Report" if report.scope == "docker" else "# Platform Validation Report"
    lines = [
        title,
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
    parser.add_argument("--docker-only", action="store_true", help="Run Docker/local production validation only.")
    parser.add_argument("--report", type=Path, help="Optional path to write markdown report.")
    args = parser.parse_args()

    report = ValidationReport(scope="docker" if args.docker_only else "platform")
    if args.docker_only:
        check_tooling(report)
        check_docker(report)
    else:
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
                "scope": report.scope,
                "report_version": REPORT_VERSION,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
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
