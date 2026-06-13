from __future__ import annotations

import argparse
from typing import Any

from trading_system.app.tasks import (
    collect_market_data,
    fetch_sec_filings,
    run_fill_reconciliation,
    run_scheduled_job,
    run_trade_monitor,
    stream_market_data,
)

_TASK_ALIASES = {
    "market-stream": stream_market_data,
    "scheduler": run_scheduled_job,
    "reconciliation": run_fill_reconciliation,
    "trade-monitor": run_trade_monitor,
    "review": run_scheduled_job,
    "reviews": run_scheduled_job,
    "learning": run_scheduled_job,
    "provider-health": run_scheduled_job,
    "missing-candle-repair": run_scheduled_job,
    "universe": run_scheduled_job,
    "live-readiness": run_scheduled_job,
    "market-data": collect_market_data,
    "sec": fetch_sec_filings,
}

_SCHEDULED_JOB_NAMES = {
    "scheduler": "all",
    "review": "reviews",
    "reviews": "reviews",
    "learning": "learning",
    "provider-health": "provider_health",
    "missing-candle-repair": "missing_candle_repair",
    "universe": "universe",
    "live-readiness": "live_readiness",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compatibility entrypoint that enqueues work on the Celery task queue."
    )
    parser.add_argument("worker", choices=sorted(_TASK_ALIASES))
    args = parser.parse_args()
    async_result = enqueue_worker(args.worker)
    print(f"Queued {args.worker} as Celery task {async_result.id}")


def enqueue_worker(worker: str) -> Any:
    task = _TASK_ALIASES[worker]
    if worker in _SCHEDULED_JOB_NAMES:
        return task.delay(_SCHEDULED_JOB_NAMES[worker])
    return task.delay()


if __name__ == "__main__":
    main()
