from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from typing import Any

from celery import Celery
from celery.schedules import crontab
from trading_system.app.core.config import get_settings
from trading_system.app.data.partition_manager import PartitionManager
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import SessionLocal
from trading_system.app.research.backtest_service import BacktestService
from trading_system.app.alpha.strategies import AlphaStrategyScannerService
from trading_system.app.execution.order_manager import OrderManager, TWAP_Order_Manager
from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.services.scheduler import _job_cadences

settings = get_settings()

app = celery = Celery(
    "trading_system",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["trading_system.app.tasks"],
)

celery.conf.update(
    task_default_queue="analytics",
    task_routes={
        "trading_system.app.tasks.collect_market_data": {"queue": "market_data"},
        "trading_system.app.tasks.stream_market_data": {"queue": "market_data"},
        "trading_system.app.tasks.collect_news": {"queue": "market_data"},
        "trading_system.app.tasks.fetch_sec_filings": {"queue": "market_data"},
        "trading_system.app.tasks.run_fill_reconciliation": {"queue": "execution"},
        "trading_system.app.tasks.run_trade_monitor": {"queue": "execution"},
        "trading_system.app.tasks.run_backtest": {"queue": "analytics"},
        "trading_system.app.tasks.run_production_scanners": {"queue": "execution"},
        "trading_system.app.tasks.run_alpha_strategy_scanner": {"queue": "execution"},
        "trading_system.app.tasks.request_bracket_order": {"queue": "execution"},
        "trading_system.app.tasks.execute_twap_child_order": {"queue": "execution"},
        "trading_system.app.tasks.maintain_db_partitions": {"queue": "analytics"},
        "trading_system.app.tasks.prune_raw_time_series_partitions": {"queue": "analytics"},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "collect-market-data": {
            "task": "trading_system.app.tasks.collect_market_data",
            "schedule": _job_cadences(settings)["market_data"],
        },
        "run-features": {
            "task": "trading_system.app.tasks.run_scheduled_job",
            "schedule": _job_cadences(settings)["features"],
            "args": ("features",),
        },
        "collect-news": {
            "task": "trading_system.app.tasks.collect_news",
            "schedule": _job_cadences(settings)["news"],
        },
        "fetch-sec-filings": {
            "task": "trading_system.app.tasks.fetch_sec_filings",
            "schedule": _job_cadences(settings)["sec"],
        },
        "run-fill-reconciliation": {
            "task": "trading_system.app.tasks.run_fill_reconciliation",
            "schedule": _job_cadences(settings)["fill_reconciliation"],
        },
        "run-trade-monitor": {
            "task": "trading_system.app.tasks.run_trade_monitor",
            "schedule": _job_cadences(settings)["trade_monitor"],
        },
        "run-reviews": {
            "task": "trading_system.app.tasks.run_scheduled_job",
            "schedule": _job_cadences(settings)["reviews"],
            "args": ("reviews",),
        },
        "run-learning": {
            "task": "trading_system.app.tasks.run_scheduled_job",
            "schedule": _job_cadences(settings)["learning"],
            "args": ("learning",),
        },
        "maintain-db-partitions": {
            "task": "trading_system.app.tasks.maintain_db_partitions",
            "schedule": crontab(hour=0, minute=15, day_of_week="sun"),
        },
        "prune-raw-time-series-partitions": {
            "task": "trading_system.app.tasks.prune_raw_time_series_partitions",
            "schedule": crontab(hour=0, minute=30),
        },
    },
)

_TASK_OPTIONS = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_jitter": True,
    "max_retries": 3,
}


@celery.task(name="trading_system.app.tasks.maintain_db_partitions", **_TASK_OPTIONS)
def maintain_db_partitions() -> dict[str, str]:
    """Create next week's PostgreSQL partitions for high-volume raw tables."""
    session = SessionLocal()
    try:
        return PartitionManager(session).create_next_week_partitions()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery.task(name="trading_system.app.tasks.prune_raw_time_series_partitions", **_TASK_OPTIONS)
def prune_raw_time_series_partitions(retention_days: int = 7) -> dict[str, list[str]]:
    """Drop raw market-data partitions older than the V1 retention window."""
    session = SessionLocal()
    try:
        return PartitionManager(session).drop_partitions_older_than(retention_days=retention_days)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _with_runtime(task_name: str, operation: Callable[[TradingRuntimeService], Any]) -> Any:
    session = SessionLocal()
    started_at = datetime.now(UTC)
    try:
        repository = TradingRepository(session)
        service = TradingRuntimeService(repository, settings=get_settings())
        service.bootstrap()
        result = operation(service)
        repository.store_worker_heartbeat(
            worker_name=task_name,
            status="HEALTHY",
            last_started_at=started_at,
            last_finished_at=datetime.now(UTC),
            last_success=True,
            reason="Celery task completed.",
            payload={"result": _serialize_result(result)},
        )
        return _serialize_result(result)
    except Exception as exc:
        TradingRepository(session).store_worker_heartbeat(
            worker_name=task_name,
            status="FAILED",
            last_started_at=started_at,
            last_finished_at=datetime.now(UTC),
            last_success=False,
            reason=str(exc),
            payload=None,
        )
        raise
    finally:
        session.close()


def _serialize_result(result: Any) -> Any:
    if is_dataclass(result) and not isinstance(result, type):
        return _serialize_result(asdict(result))
    if isinstance(result, (datetime, date)):
        return result.isoformat()
    if isinstance(result, list):
        return [_serialize_result(item) for item in result]
    if isinstance(result, tuple):
        return [_serialize_result(item) for item in result]
    if isinstance(result, dict):
        return {str(key): _serialize_result(value) for key, value in result.items()}
    if hasattr(result, "__dict__"):
        return _serialize_result(result.__dict__)
    return result


@celery.task(name="trading_system.app.tasks.run_scheduled_job", **_TASK_OPTIONS)
def run_scheduled_job(
    job_name: str,
    symbols: list[str] | None = None,
    actor: str = "celery",
) -> Any:
    return _with_runtime(
        job_name,
        lambda service: service.run_scheduled_job(job_name, symbols=symbols, actor=actor),
    )


@celery.task(name="trading_system.app.tasks.collect_market_data", **_TASK_OPTIONS)
def collect_market_data(symbols: list[str] | None = None) -> Any:
    return run_scheduled_job.run("market_data", symbols=symbols)


@celery.task(name="trading_system.app.tasks.collect_news", **_TASK_OPTIONS)
def collect_news(symbols: list[str] | None = None) -> Any:
    return run_scheduled_job.run("news", symbols=symbols)


@celery.task(name="trading_system.app.tasks.fetch_sec_filings", **_TASK_OPTIONS)
def fetch_sec_filings(symbols: list[str] | None = None) -> Any:
    return run_scheduled_job.run("sec", symbols=symbols)


@celery.task(name="trading_system.app.tasks.run_fill_reconciliation", **_TASK_OPTIONS)
def run_fill_reconciliation() -> Any:
    return _with_runtime(
        "fill_reconciliation",
        lambda service: service.run_fill_reconciliation_once(),
    )


@celery.task(name="trading_system.app.tasks.run_trade_monitor", **_TASK_OPTIONS)
def run_trade_monitor() -> Any:
    return _with_runtime("trade_monitor", lambda service: service.run_trade_monitor())


@celery.task(name="trading_system.app.tasks.stream_market_data", **_TASK_OPTIONS)
def stream_market_data(max_messages: int | None = None) -> Any:
    return _with_runtime(
        "market_stream",
        lambda service: asyncio.run(
            service.run_alpaca_market_data_stream(
                max_messages=max_messages or service.settings.alpaca_stream_max_messages or None
            )
        ),
    )


@celery.task(name="trading_system.app.tasks.run_backtest", **_TASK_OPTIONS)
def run_backtest(
    symbols: list[str] | None = None,
    provider: str = "alpaca_market_data",
) -> Any:
    return _with_runtime(
        "backtest",
        lambda service: BacktestService(service.repository).run_vwap_reclaim(
            symbols=symbols,
            provider=provider,
        ),
    )


@celery.task(bind=True, name="trading_system.app.tasks.run_production_scanners", **_TASK_OPTIONS)
def run_production_scanners(
    self,
    symbols: list[str] | None = None,
    actor: str = "celery",
) -> Any:
    return _with_runtime(
        "production_scanners",
        lambda service: service.run_production_scanners(symbols),
    )


@celery.task(bind=True, name="trading_system.app.tasks.run_alpha_strategy_scanner", **_TASK_OPTIONS)
def run_alpha_strategy_scanner(
    self,
    strategy_id: str,
    symbols: list[str] | None = None,
    actor: str = "celery",
) -> Any:
    return _with_runtime(
        "alpha_scanner_run",
        lambda service: AlphaStrategyScannerService(service.repository).run_strategy(
            strategy_id, symbols=symbols
        ),
    )


@celery.task(bind=True, name="trading_system.app.tasks.request_bracket_order", **_TASK_OPTIONS)
def request_bracket_order(self, **kwargs: Any) -> Any:
    return _with_runtime(
        "request_bracket_order",
        lambda service: OrderManager(service.repository).request_bracket_order(**kwargs),
    )


@celery.task(bind=True, name="trading_system.app.tasks.execute_twap_child_order", **_TASK_OPTIONS)
def execute_twap_child_order(
    self, previous_result: dict[str, Any] | None = None, **kwargs: Any
) -> Any:
    return _with_runtime(
        "execute_twap_child_order",
        lambda service: TWAP_Order_Manager(service.repository).execute_child_order(
            previous_result, **kwargs
        ),
    )
