from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

import httpx


T = TypeVar("T")

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    value: T
    attempts: int


async def request_with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> RetryResult[T]:
    attempts = max(1, max_attempts)
    last_exc: httpx.HTTPError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return RetryResult(await operation(), attempt)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= attempts or not is_retryable_request_error(exc):
                raise
            if backoff_seconds > 0:
                await asyncio.sleep(backoff_seconds * attempt)
    raise last_exc or RuntimeError("Retry operation failed without an exception.")


def is_retryable_request_error(exc: httpx.HTTPError) -> bool:
    if isinstance(
        exc,
        (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.NetworkError),
    ):
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in RETRYABLE_STATUS_CODES
