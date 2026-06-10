from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.services.scheduler import (
    ScheduledCollectorRunner,
    ScheduledJobResult,
    _cadence_elapsed,
)


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _store_run(repo: TradingRepository, job_name: str, finished_at: datetime) -> None:
    repo.store_scheduler_run(
        job_name=job_name,
        success=True,
        started_at=finished_at - timedelta(seconds=1),
        finished_at=finished_at,
        reason="cadence test seed",
    )


class _RecordingRunner(ScheduledCollectorRunner):
    """Records which child jobs the ``"all"`` fan-out actually dispatches."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.dispatched: list[str] = []

    def run_once(self, job_name, *, symbols=None, actor="system"):  # type: ignore[override]
        if job_name.strip().lower() == "all":
            return super().run_once(job_name, symbols=symbols, actor=actor)
        self.dispatched.append(job_name)
        return ScheduledJobResult(job_name, True, "stubbed child", {})


def test_cadence_elapsed_helper():
    now = datetime.now(UTC)
    assert _cadence_elapsed(now, None, 60) is True
    assert _cadence_elapsed(now, now, 60) is False
    assert _cadence_elapsed(now, now - timedelta(seconds=120), 60) is True
    # Naive timestamps are coerced to UTC rather than raising.
    assert _cadence_elapsed(now, (now - timedelta(seconds=120)).replace(tzinfo=None), 60) is True


def test_last_scheduler_run_times_returns_latest_per_job():
    repo = _repo()
    now = datetime.now(UTC)
    _store_run(repo, "news", now - timedelta(hours=2))
    _store_run(repo, "news", now - timedelta(minutes=1))
    _store_run(repo, "market_data", now - timedelta(seconds=5))

    last = repo.last_scheduler_run_times()

    assert set(last) == {"news", "market_data"}
    assert (now - last["news"].replace(tzinfo=last["news"].tzinfo or UTC)).total_seconds() < 120


def test_all_path_skips_jobs_within_cadence_but_runs_due_jobs():
    repo = _repo()
    now = datetime.now(UTC)
    # news ran just now (cadence 300s) -> must be skipped.
    _store_run(repo, "news", now)
    # market_data ran long ago (cadence 60s) -> must run again.
    _store_run(repo, "market_data", now - timedelta(days=1))
    # regime has never run -> must run.

    runner = _RecordingRunner(repo, settings=Settings())
    result = runner.run_once("all")

    assert "news" not in runner.dispatched
    assert "market_data" in runner.dispatched
    assert "regime" in runner.dispatched
    assert result.payload["news"]["skipped"] is True
    assert "market_data" not in result.payload or result.payload["market_data"].get("skipped") is not True


def test_all_path_runs_everything_on_first_cycle():
    repo = _repo()
    runner = _RecordingRunner(repo, settings=Settings())

    runner.run_once("all")

    # With no prior runs persisted, every cadence-gated job is due on the first cycle.
    for job in ("market_data", "news", "sec", "regime", "reviews", "learning"):
        assert job in runner.dispatched
