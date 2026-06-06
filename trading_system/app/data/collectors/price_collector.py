from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.data.validators.market_data import MarketDataRecord, validate_market_data_record


class RawMarketDataRepository(Protocol):
    def store_raw_candle(self, payload: dict) -> str:
        ...

    def store_clean_candle(self, payload: dict) -> str:
        ...


@dataclass(frozen=True)
class CollectionResult:
    raw_id: str
    clean_id: str | None
    quality_status: str
    reason: str


class PriceDataCollector:
    """Raw-first collector boundary. Provider-specific clients plug in above this layer."""

    def __init__(self, repository: RawMarketDataRepository) -> None:
        self.repository = repository

    def ingest_candle(
        self,
        record: MarketDataRecord,
        *,
        raw_payload: dict,
        now: datetime,
        expected_regular_session: bool = False,
        duplicate: bool = False,
    ) -> CollectionResult:
        assert_provider_usage(record.provider, research=True, intraday=True)
        raw_id = self.repository.store_raw_candle(
            {
                "provider": record.provider,
                "symbol": record.symbol,
                "timeframe": record.timeframe,
                "source_timestamp": record.source_timestamp,
                "raw_payload": raw_payload,
            }
        )

        validation = validate_market_data_record(
            record,
            now=now,
            duplicate=duplicate,
            expected_regular_session=expected_regular_session,
        )
        clean_id = None
        if validation.is_valid:
            clean_payload = asdict(record)
            clean_payload["raw_market_data_id"] = raw_id
            clean_payload["data_quality_status"] = validation.status.value
            clean_payload["quality_reason"] = validation.reason
            clean_id = self.repository.store_clean_candle(clean_payload)

        return CollectionResult(
            raw_id=raw_id,
            clean_id=clean_id,
            quality_status=validation.status.value,
            reason=validation.reason,
        )

