from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime

from trading_system.app.core.config import get_settings
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import SessionLocal
from trading_system.app.services.runtime import TradingRuntimeService


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading platform worker entrypoint.")
    parser.add_argument(
        "worker",
        choices=[
            "market-stream",
            "scheduler",
            "reconciliation",
            "trade-monitor",
            "review",
            "reviews",
            "learning",
            "provider-health",
            "missing-candle-repair",
            "universe",
            "live-readiness",
        ],
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    args = parser.parse_args()
    settings = get_settings()
    while True:
        session = SessionLocal()
        try:
            service = TradingRuntimeService(TradingRepository(session), settings=settings)
            service.bootstrap()
            started_at = datetime.now(UTC)
            _run_worker(args.worker, service)
            service.repository.store_worker_heartbeat(
                worker_name=args.worker,
                status="HEALTHY",
                last_started_at=started_at,
                last_finished_at=datetime.now(UTC),
                last_success=True,
                reason="Worker cycle completed.",
                payload=None,
            )
        except Exception as exc:
            TradingRepository(session).store_worker_heartbeat(
                worker_name=args.worker,
                status="FAILED",
                last_started_at=locals().get("started_at"),
                last_finished_at=datetime.now(UTC),
                last_success=False,
                reason=str(exc),
                payload=None,
            )
            raise
        finally:
            session.close()
        if args.once:
            break
        time.sleep(max(1, settings.worker_sleep_seconds))


def _run_worker(worker: str, service: TradingRuntimeService) -> None:
    if worker == "market-stream":
        max_messages = service.settings.alpaca_stream_max_messages or None
        asyncio.run(service.run_alpaca_market_data_stream(max_messages=max_messages))
    elif worker == "scheduler":
        service.run_scheduled_job("all")
    elif worker == "reconciliation":
        service.run_fill_reconciliation_once()
    elif worker == "trade-monitor":
        service.run_trade_monitor()
    elif worker in {"review", "reviews"}:
        service.run_reviews()
    elif worker == "learning":
        service.run_learning_review()
    elif worker == "provider-health":
        service.run_provider_health()
    elif worker == "missing-candle-repair":
        service.repair_missing_candles()
    elif worker == "universe":
        service.refresh_universe()
    elif worker == "live-readiness":
        service.generate_live_readiness_report()
    else:
        raise ValueError(f"Unknown worker: {worker}")


if __name__ == "__main__":
    main()
