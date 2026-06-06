from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


PROVIDER_HEALTH_VERSION = "provider_health_v1"


@dataclass(frozen=True)
class ProviderHealthRunResult:
    providers_checked: int
    unhealthy_count: int
    reason: str
    version: str = PROVIDER_HEALTH_VERSION


class ProviderHealthService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def run_once(self) -> ProviderHealthRunResult:
        providers = self.repository.session.scalars(select(models.ProviderCapability)).all()
        unhealthy = 0
        for provider in providers:
            snapshot = self._build_snapshot(provider.provider_name)
            if snapshot["status"] != "HEALTHY":
                unhealthy += 1
            self.repository.store_provider_health_snapshot(**snapshot)
        return ProviderHealthRunResult(
            providers_checked=len(providers),
            unhealthy_count=unhealthy,
            reason="Provider health snapshots recorded.",
        )

    def _build_snapshot(self, provider_name: str) -> dict:
        now = datetime.now(UTC)
        api_logs = self.repository.session.scalars(
            select(models.ApiCallLog)
            .where(models.ApiCallLog.provider == provider_name)
            .order_by(desc(models.ApiCallLog.created_at))
            .limit(20)
        ).all()
        latest_success = next((row for row in api_logs if row.success), None)
        latest_failure = next((row for row in api_logs if not row.success), None)
        failure_streak = 0
        for row in api_logs:
            if row.success:
                break
            failure_streak += 1
        latest_data_ts = self._latest_data_timestamp(provider_name)
        freshness_seconds = (
            (now - latest_data_ts).total_seconds()
            if latest_data_ts and latest_data_ts.tzinfo is not None
            else None
        )
        latency_ms = api_logs[0].duration_ms if api_logs else None
        status = "HEALTHY"
        reason = "Provider has recent successful activity."
        if failure_streak >= 3:
            status = "DEGRADED"
            reason = "Provider has three or more consecutive failures."
        if freshness_seconds is not None and freshness_seconds > self.settings.provider_health_max_age_seconds:
            status = "STALE"
            reason = "Provider data is older than configured freshness threshold."
        if not api_logs and latest_data_ts is None:
            status = "UNKNOWN"
            reason = "Provider has no API or data activity yet."
        reliability_score = _score(status, failure_streak)
        return {
            "provider_name": provider_name,
            "status": status,
            "last_success_at": latest_success.source_timestamp if latest_success else None,
            "last_failure_at": latest_failure.source_timestamp if latest_failure else None,
            "failure_streak": failure_streak,
            "latency_ms": latency_ms,
            "freshness_seconds": freshness_seconds,
            "reliability_score": reliability_score,
            "reason": reason,
            "payload": {"version": PROVIDER_HEALTH_VERSION},
            "source_timestamp": now,
        }

    def _latest_data_timestamp(self, provider_name: str) -> datetime | None:
        candle = self.repository.session.scalar(
            select(models.CleanMarketData)
            .where(models.CleanMarketData.provider == provider_name)
            .order_by(desc(models.CleanMarketData.source_timestamp))
            .limit(1)
        )
        stream = self.repository.session.scalar(
            select(models.MarketDataStreamEvent)
            .where(models.MarketDataStreamEvent.provider == provider_name)
            .order_by(desc(models.MarketDataStreamEvent.source_timestamp))
            .limit(1)
        )
        timestamps = [
            item.source_timestamp
            for item in [candle, stream]
            if item is not None and item.source_timestamp is not None
        ]
        return max(timestamps) if timestamps else None


def _score(status: str, failure_streak: int) -> float:
    if status == "HEALTHY":
        return max(70.0, 100.0 - failure_streak * 10)
    if status == "STALE":
        return 45.0
    if status == "DEGRADED":
        return 35.0
    return 0.0
