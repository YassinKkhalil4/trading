from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

import redis

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
            client = redis.from_url(self.settings.redis_url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None


def _normalize_key(key: str) -> str:
    cleaned = key.strip().lower().replace(" ", "_")
    return cleaned if cleaned.startswith("trading:") else f"trading:{cleaned}"


class DistributedLockTimeout(TimeoutError):
    """Raised when a distributed lock cannot be acquired before its wait timeout."""


class DistributedLock:
    """Redis-backed context manager for short critical sections across workers.

    The lock uses Redis' atomic SET NX semantics through redis-py's Lock implementation,
    blocks for a bounded period while another worker holds the lock, and always applies
    a TTL so a crashed worker cannot deadlock live execution indefinitely.
    """

    def __init__(
        self,
        redis_client,
        name: str,
        *,
        blocking_timeout: float = 10.0,
        ttl_seconds: float = 30.0,
    ) -> None:
        self.redis_client = redis_client
        self.name = _normalize_key(name)
        self.blocking_timeout = blocking_timeout
        self.ttl_seconds = ttl_seconds
        self._lock = None
        self._memory_token: str | None = None

    def __enter__(self) -> "DistributedLock":
        self._lock = self.redis_client.lock(
            self.name,
            timeout=self.ttl_seconds,
            blocking=True,
            blocking_timeout=self.blocking_timeout,
            thread_local=False,
        )
        try:
            acquired = bool(self._lock.acquire())
        except redis.RedisError:
            self._lock = None
            return self._acquire_memory_fallback()
        if not acquired:
            self._lock = None
            raise DistributedLockTimeout(
                f"Timed out after {self.blocking_timeout}s waiting for distributed lock {self.name}."
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._lock is not None:
            try:
                self._lock.release()
            finally:
                self._lock = None
        if self._memory_token is not None:
            existing = _MEMORY_LOCKS.get(self.name)
            if existing and existing[1] == self._memory_token:
                _MEMORY_LOCKS.pop(self.name, None)
            self._memory_token = None
        return False

    def _acquire_memory_fallback(self) -> "DistributedLock":
        deadline = time.monotonic() + self.blocking_timeout
        token = str(uuid4())
        while True:
            now = time.time()
            existing = _MEMORY_LOCKS.get(self.name)
            if existing and existing[0] <= now:
                _MEMORY_LOCKS.pop(self.name, None)
                existing = None
            if not existing:
                _MEMORY_LOCKS[self.name] = (now + self.ttl_seconds, token)
                self._memory_token = token
                return self
            if time.monotonic() >= deadline:
                raise DistributedLockTimeout(
                    f"Timed out after {self.blocking_timeout}s waiting for fallback lock {self.name}."
                )
            time.sleep(0.1)


def redis_client_from_settings(settings: Settings | None = None) -> redis.Redis:
    resolved_settings = settings or get_settings()
    return redis.from_url(resolved_settings.redis_url, decode_responses=True)
