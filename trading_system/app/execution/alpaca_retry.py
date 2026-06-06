from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import sleep
from typing import Generic, TypeVar

import requests


T = TypeVar("T")

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    value: T
    attempts: int


def request_with_retries(
    operation: Callable[[], T],
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> RetryResult[T]:
    attempts = max(1, max_attempts)
    last_exc: requests.RequestException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return RetryResult(operation(), attempt)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= attempts or not is_retryable_request_error(exc):
                raise
            if backoff_seconds > 0:
                sleep(backoff_seconds * attempt)
    raise last_exc or RuntimeError("Retry operation failed without an exception.")


def is_retryable_request_error(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in RETRYABLE_STATUS_CODES
