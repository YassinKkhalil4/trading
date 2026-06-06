from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


PROVIDER_CACHE_VERSION = "provider_cache_v1"
_MEMORY_CACHE: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str
    remaining: int | None
    reset_at_epoch: float | None
    version: str = PROVIDER_CACHE_VERSION


class ProviderResponseCache:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = self._redis_client()

    def get_json(self, key: str) -> dict[str, Any] | None:
        if self.client:
            try:
                raw = self.client.get(key)
                return json.loads(raw) if raw else None
            except Exception:
                self.client = None
        item = _MEMORY_CACHE.get(key)
        if not item:
            return None
        expires_at, raw = item
        if expires_at < time.time():
            _MEMORY_CACHE.pop(key, None)
            return None
        return json.loads(raw)

    def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        raw = json.dumps(value, default=str)
        if self.client:
            try:
                self.client.setex(key, ttl_seconds, raw)
                return
            except Exception:
                self.client = None
        _MEMORY_CACHE[key] = (time.time() + ttl_seconds, raw)

    def _redis_client(self):
        try:
            import redis
        except ImportError:
            return None
        try:
            return redis.from_url(self.settings.redis_url, decode_responses=True)
        except Exception:
            return None


class ProviderRateLimiter:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def record(
        self,
        *,
        provider_name: str,
        endpoint: str,
        limit_remaining: int | None,
        reset_at_epoch: float | None,
        reason: str,
    ) -> None:
        from datetime import UTC, datetime

        reset_at = (
            datetime.fromtimestamp(reset_at_epoch, tz=UTC)
            if reset_at_epoch is not None
            else None
        )
        self.repository.store_provider_rate_limit_state(
            provider_name=provider_name,
            endpoint=endpoint,
            limit_remaining=limit_remaining,
            reset_at=reset_at,
            request_count=1,
            blocked_until=reset_at if limit_remaining == 0 else None,
            reason=reason,
        )

    def allow(self, *, provider_name: str, endpoint: str) -> RateLimitDecision:
        rows = self.repository.list_rows(models.ProviderRateLimitState, 20)
        now = time.time()
        for row in rows:
            if row["provider_name"] != provider_name or row["endpoint"] != endpoint:
                continue
            blocked_until = row.get("blocked_until")
            if blocked_until and getattr(blocked_until, "timestamp", None) and blocked_until.timestamp() > now:
                return RateLimitDecision(False, "Provider endpoint is rate-limit blocked.", row.get("limit_remaining"), blocked_until.timestamp())
        return RateLimitDecision(True, "Provider endpoint is allowed.", None, None)
