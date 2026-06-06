from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DataQualityStatus
from trading_system.app.data.provider_cache import ProviderRateLimiter, ProviderResponseCache
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.data.validators.market_data import MarketDataRecord, validate_market_data_record
from trading_system.app.db.repositories import TradingRepository


ALPACA_MARKET_DATA_PROVIDER = "alpaca_market_data"


@dataclass(frozen=True)
class AlpacaBarsResult:
    configured: bool
    success: bool
    symbol: str
    candles_seen: int
    raw_stored: int
    clean_stored: int
    invalid_stored: int
    reason: str


class AlpacaBarsCollector:
    """Primary Alpaca REST bar collector for production paper/research use."""

    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.http = http or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_paper_api_key and self.settings.alpaca_paper_secret_key)

    def collect(
        self,
        symbol: str,
        *,
        timeframe: str | None = None,
        limit: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> AlpacaBarsResult:
        assert_provider_usage(ALPACA_MARKET_DATA_PROVIDER, research=True, intraday=True)
        normalized = symbol.strip().upper()
        if not self.configured:
            return AlpacaBarsResult(
                False,
                False,
                normalized,
                0,
                0,
                0,
                0,
                "Alpaca paper/data keys are not configured.",
            )

        timeframe = timeframe or self.settings.alpaca_bars_timeframe
        limit = limit or self.settings.alpaca_bars_limit
        end = end or datetime.now(UTC)
        start = start or (end - timedelta(days=5))
        endpoint = f"{self.settings.alpaca_paper_data_url}/v2/stocks/{normalized}/bars"
        params = {
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": str(limit),
            "feed": self.settings.alpaca_primary_data_feed,
            "sort": "asc",
        }
        request_hash = hashlib.sha256(f"{endpoint}:{params}".encode("utf-8")).hexdigest()
        rate_limit = ProviderRateLimiter(self.repository).allow(
            provider_name=ALPACA_MARKET_DATA_PROVIDER,
            endpoint=endpoint,
        )
        if not rate_limit.allowed:
            return AlpacaBarsResult(True, False, normalized, 0, 0, 0, 0, rate_limit.reason)
        cache = ProviderResponseCache(self.settings)
        cached_payload = cache.get_json(f"alpaca_bars:{request_hash}")
        if cached_payload is not None:
            self.repository.log_api_call(
                provider=ALPACA_MARKET_DATA_PROVIDER,
                endpoint=endpoint,
                status_code=200,
                success=True,
                reason="Alpaca bars response served from provider cache.",
                duration_ms=0.0,
                request_hash=request_hash,
            )
            payload = cached_payload
        else:
            payload = None
        started = time.perf_counter()
        if payload is None:
            try:
                response = self.http.get(endpoint, headers=self._headers(), params=params, timeout=20)
                duration_ms = (time.perf_counter() - started) * 1000
                self._record_rate_limit_headers(response, endpoint=endpoint)
                self.repository.log_api_call(
                    provider=ALPACA_MARKET_DATA_PROVIDER,
                    endpoint=endpoint,
                    status_code=response.status_code,
                    success=response.ok,
                    reason="Alpaca bars request succeeded." if response.ok else "Alpaca bars request failed.",
                    duration_ms=duration_ms,
                    request_hash=request_hash,
                )
                response.raise_for_status()
                payload = response.json()
                cache.set_json(f"alpaca_bars:{request_hash}", payload, ttl_seconds=30)
            except requests.RequestException as exc:
                duration_ms = (time.perf_counter() - started) * 1000
                self.repository.log_api_call(
                    provider=ALPACA_MARKET_DATA_PROVIDER,
                    endpoint=endpoint,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                    success=False,
                    reason=f"Alpaca bars request error: {exc}",
                    duration_ms=duration_ms,
                    request_hash=request_hash,
                )
                return AlpacaBarsResult(True, False, normalized, 0, 0, 0, 0, str(exc))

        bars = payload.get("bars") or []
        raw_stored = clean_stored = invalid_stored = 0
        for bar in bars:
            ts = _parse_time(bar.get("t")) or datetime.now(UTC)
            raw_id = self.repository.store_raw_candle(
                {
                    "provider": ALPACA_MARKET_DATA_PROVIDER,
                    "symbol": normalized,
                    "timeframe": timeframe,
                    "source_timestamp": ts,
                    "raw_payload": bar,
                }
            )
            raw_stored += 1
            record = MarketDataRecord(
                provider=ALPACA_MARKET_DATA_PROVIDER,
                symbol=normalized,
                timeframe=timeframe,
                source_timestamp=ts,
                open=float(bar.get("o")),
                high=float(bar.get("h")),
                low=float(bar.get("l")),
                close=float(bar.get("c")),
                volume=float(bar.get("v") or 0),
                trade_count=int(bar.get("n") or 0) if bar.get("n") is not None else None,
                vwap=float(bar.get("vw")) if bar.get("vw") is not None else None,
            )
            validation = validate_market_data_record(record, now=ts, expected_regular_session=False)
            self.repository.store_clean_candle(
                {
                    "raw_market_data_id": raw_id,
                    "provider": ALPACA_MARKET_DATA_PROVIDER,
                    "symbol": normalized,
                    "timeframe": timeframe,
                    "source_timestamp": ts,
                    "open": record.open,
                    "high": record.high,
                    "low": record.low,
                    "close": record.close,
                    "volume": record.volume,
                    "trade_count": record.trade_count,
                    "vwap": record.vwap,
                    "data_quality_status": validation.status.value,
                    "quality_reason": validation.reason,
                }
            )
            clean_stored += 1
            if validation.status != DataQualityStatus.VALID:
                invalid_stored += 1
                self.repository.store_data_quality_error(
                    provider=ALPACA_MARKET_DATA_PROVIDER,
                    symbol=normalized,
                    timeframe=timeframe,
                    data_quality_status=validation.status.value,
                    reason=validation.reason,
                    source_timestamp=ts,
                    payload=bar,
                )

        return AlpacaBarsResult(
            True,
            True,
            normalized,
            len(bars),
            raw_stored,
            clean_stored,
            invalid_stored,
            "Alpaca bars ingested raw-first.",
        )

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_paper_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_paper_secret_key,
        }

    def _record_rate_limit_headers(self, response: requests.Response, *, endpoint: str) -> None:
        remaining = _int_or_none(
            response.headers.get("X-RateLimit-Remaining")
            or response.headers.get("x-ratelimit-remaining")
        )
        reset_at_epoch = _float_or_none(
            response.headers.get("X-RateLimit-Reset")
            or response.headers.get("x-ratelimit-reset")
        )
        if remaining is None and reset_at_epoch is None:
            return
        ProviderRateLimiter(self.repository).record(
            provider_name=ALPACA_MARKET_DATA_PROVIDER,
            endpoint=endpoint,
            limit_remaining=remaining,
            reset_at_epoch=reset_at_epoch,
            reason="Alpaca bars rate-limit headers recorded.",
        )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
