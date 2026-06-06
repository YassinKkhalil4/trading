from __future__ import annotations

import hashlib
from datetime import datetime


class DuplicateIdempotencyKeyError(RuntimeError):
    pass


class IdempotencyRegistry:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def reserve(self, key: str) -> None:
        if key in self._keys:
            raise DuplicateIdempotencyKeyError(f"Duplicate idempotency key rejected: {key}")
        self._keys.add(key)

    def exists(self, key: str) -> bool:
        return key in self._keys


def build_idempotency_key(
    *,
    namespace: str,
    symbol: str,
    strategy_id: str,
    source_timestamp: datetime,
    direction: str,
) -> str:
    raw = f"{namespace}:{symbol}:{strategy_id}:{source_timestamp.isoformat()}:{direction}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

