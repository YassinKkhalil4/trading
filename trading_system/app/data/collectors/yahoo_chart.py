from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from trading_system.app.core.enums import DataQualityStatus
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.data.validators.market_data import (
    MarketDataRecord,
    validate_market_data_record,
)
from trading_system.app.db.repositories import TradingRepository


YAHOO_CHART_PROVIDER = "yahoo_chart"


@dataclass(frozen=True)
class YahooChartResult:
    symbol: str
    candles_seen: int
    raw_stored: int
    clean_stored: int
    invalid_stored: int
    reason: str


class YahooChartCollector:
    """Research-grade collector using Yahoo's public chart endpoint.

    This is deliberately marked as a research fallback. It provides real candles
    for the dashboard, but it is not execution-grade data and cannot unlock live trading.
    """

    def __init__(self, repository: TradingRepository, http: requests.Session | None = None) -> None:
        self.repository = repository
        self.http = http or requests.Session()

    def collect(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        range_: str = "1d",
        include_prepost: bool = True,
        timeout_seconds: int = 15,
    ) -> YahooChartResult:
        assert_provider_usage(YAHOO_CHART_PROVIDER, research=True, intraday=True)
        normalized = symbol.strip().upper()
        endpoint = f"https://query1.finance.yahoo.com/v8/finance/chart/{normalized}"
        params = {
            "interval": interval,
            "range": range_,
            "includePrePost": "true" if include_prepost else "false",
            "events": "div,splits",
        }
        request_hash = hashlib.sha256(f"{endpoint}:{params}".encode("utf-8")).hexdigest()
        started = time.perf_counter()
        try:
            response = self.http.get(endpoint, params=params, timeout=timeout_seconds)
            duration_ms = (time.perf_counter() - started) * 1000
            if response.status_code != 200:
                self.repository.log_api_call(
                    provider=YAHOO_CHART_PROVIDER,
                    endpoint=endpoint,
                    status_code=response.status_code,
                    success=False,
                    reason=f"Yahoo chart request failed with HTTP {response.status_code}.",
                    duration_ms=duration_ms,
                    request_hash=request_hash,
                )
                return YahooChartResult(normalized, 0, 0, 0, 0, "Yahoo chart request failed.")
            payload = response.json()
            self.repository.log_api_call(
                provider=YAHOO_CHART_PROVIDER,
                endpoint=endpoint,
                status_code=response.status_code,
                success=True,
                reason="Yahoo chart request succeeded.",
                duration_ms=duration_ms,
                request_hash=request_hash,
            )
        except requests.RequestException as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self.repository.log_api_call(
                provider=YAHOO_CHART_PROVIDER,
                endpoint=endpoint,
                status_code=None,
                success=False,
                reason=f"Yahoo chart request error: {exc}",
                duration_ms=duration_ms,
                request_hash=request_hash,
            )
            return YahooChartResult(normalized, 0, 0, 0, 0, str(exc))

        candles = self._extract_candles(normalized, interval, payload)
        raw_stored = clean_stored = invalid_stored = 0
        for candle in candles:
            raw_id = self.repository.store_raw_candle(
                {
                    "provider": YAHOO_CHART_PROVIDER,
                    "symbol": normalized,
                    "timeframe": self._normalize_interval(interval),
                    "source_timestamp": candle.source_timestamp,
                    "raw_payload": candle.raw_payload,
                }
            )
            raw_stored += 1

            record = candle.to_record(provider=YAHOO_CHART_PROVIDER, timeframe=self._normalize_interval(interval))
            validation = validate_market_data_record(
                record,
                now=candle.source_timestamp,
                expected_regular_session=False,
                duplicate=False,
            )
            clean_payload = {
                "raw_market_data_id": raw_id,
                "provider": YAHOO_CHART_PROVIDER,
                "symbol": normalized,
                "timeframe": self._normalize_interval(interval),
                "source_timestamp": candle.source_timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": validation.status.value,
                "quality_reason": validation.reason,
            }
            self.repository.store_clean_candle(clean_payload)
            clean_stored += 1
            if validation.status != DataQualityStatus.VALID:
                invalid_stored += 1
                self.repository.store_data_quality_error(
                    provider=YAHOO_CHART_PROVIDER,
                    symbol=normalized,
                    timeframe=self._normalize_interval(interval),
                    data_quality_status=validation.status.value,
                    reason=validation.reason,
                    source_timestamp=candle.source_timestamp,
                    payload=candle.raw_payload,
                )

        return YahooChartResult(
            symbol=normalized,
            candles_seen=len(candles),
            raw_stored=raw_stored,
            clean_stored=clean_stored,
            invalid_stored=invalid_stored,
            reason="Yahoo chart candles ingested raw-first.",
        )

    @staticmethod
    def _normalize_interval(interval: str) -> str:
        return {"1m": "1Min", "5m": "5Min", "1d": "1D"}.get(interval, interval)

    @staticmethod
    def _extract_candles(symbol: str, interval: str, payload: dict[str, Any]) -> list["_YahooCandle"]:
        chart = payload.get("chart", {})
        error = chart.get("error")
        if error:
            raise ValueError(f"Yahoo chart error for {symbol}: {error}")
        results = chart.get("result") or []
        if not results:
            return []
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        candles: list[_YahooCandle] = []
        for idx, unix_ts in enumerate(timestamps):
            values = {
                "open": _safe_number(opens, idx),
                "high": _safe_number(highs, idx),
                "low": _safe_number(lows, idx),
                "close": _safe_number(closes, idx),
                "volume": _safe_number(volumes, idx, default=0.0),
            }
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            source_timestamp = datetime.fromtimestamp(int(unix_ts), tz=UTC)
            candles.append(
                _YahooCandle(
                    symbol=symbol,
                    source_timestamp=source_timestamp,
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"] or 0.0),
                    raw_payload={
                        "source": "yahoo_chart",
                        "interval": interval,
                        "timestamp": unix_ts,
                        **values,
                    },
                )
            )
        return candles


@dataclass(frozen=True)
class _YahooCandle:
    symbol: str
    source_timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    raw_payload: dict[str, Any]

    def to_record(self, *, provider: str, timeframe: str) -> MarketDataRecord:
        return MarketDataRecord(
            provider=provider,
            symbol=self.symbol,
            timeframe=timeframe,
            source_timestamp=self.source_timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


def _safe_number(values: list[Any], idx: int, default: float | None = None) -> float | None:
    if idx >= len(values):
        return default
    value = values[idx]
    if value is None:
        return default
    return float(value)

