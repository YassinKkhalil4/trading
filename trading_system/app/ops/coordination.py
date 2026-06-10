from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

from trading_system.app.core.config import Settings, get_settings


COORDINATION_VERSION = "redis_coordination_v1"
_MEMORY_LOCKS: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class LockHandle:
    key: str
    token: str
    acquired: bool
    backend: str
    ttl_seconds: int
    reason: str
    degraded: bool = False
    version: str = COORDINATION_VERSION


class CoordinationLockManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = self._redis_client()

    def acquire(self, key: str, *, ttl_seconds: int | None = None) -> LockHandle:
        normalized = _normalize_key(key)
        ttl = ttl_seconds or self.settings.scheduler_lock_ttl_seconds
        token = str(uuid4())
        if self.client:
            try:
                acquired = bool(self.client.set(normalized, token, ex=ttl, nx=True))
                return LockHandle(
                    normalized,
                    token,
                    acquired,
                    "redis",
                    ttl,
                    "Redis coordination lock acquired." if acquired else "Redis coordination lock is already held.",
                )
            except Exception as exc:
                self.client = None
                return self._acquire_memory(
                    normalized,
                    token,
                    ttl,
                    degraded=True,
                    degraded_reason=f"Redis unavailable ({exc}); coordination degraded to single-node in-memory lock.",
                )
        return self._acquire_memory(normalized, token, ttl)

    def release(self, handle: LockHandle) -> bool:
        if not handle.acquired:
            return False
        if handle.backend == "redis" and self.client:
            try:
                script = (
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end"
                )
                return bool(self.client.eval(script, 1, handle.key, handle.token))
            except Exception:
                self.client = None
                return False
        return self._release_memory(handle)

    def _acquire_memory(
        self,
        key: str,
        token: str,
        ttl_seconds: int,
        *,
        degraded: bool = False,
        degraded_reason: str | None = None,
    ) -> LockHandle:
        now = time.time()
        prefix = f"{degraded_reason} " if degraded and degraded_reason else ""
        existing = _MEMORY_LOCKS.get(key)
        if existing:
            expires_at, _existing_token = existing
            if expires_at > now:
                return LockHandle(
                    key,
                    token,
                    False,
                    "memory",
                    ttl_seconds,
                    f"{prefix}In-memory coordination lock is already held.",
                    degraded=degraded,
                )
            _MEMORY_LOCKS.pop(key, None)
        _MEMORY_LOCKS[key] = (now + ttl_seconds, token)
        return LockHandle(
            key,
            token,
            True,
            "memory",
            ttl_seconds,
            f"{prefix}In-memory coordination lock acquired.",
            degraded=degraded,
        )

    def _release_memory(self, handle: LockHandle) -> bool:
        existing = _MEMORY_LOCKS.get(handle.key)
        if not existing:
            return False
        _expires_at, token = existing
        if token != handle.token:
            return False
        _MEMORY_LOCKS.pop(handle.key, None)
        return True

    def _redis_client(self):
        try:
            import redis
        except ImportError:
            return None
        try:
            client = redis.from_url(self.settings.redis_url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None


def _normalize_key(key: str) -> str:
    cleaned = key.strip().lower().replace(" ", "_")
    return cleaned if cleaned.startswith("trading:") else f"trading:{cleaned}"
