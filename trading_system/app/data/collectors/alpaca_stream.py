from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trading_system.app.catalysts.news_classifier import classify_news_headline
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DataQualityStatus
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.data.validators.market_data import MarketDataRecord, validate_market_data_record
from trading_system.app.db.repositories import TradingRepository


ALPACA_MARKET_DATA_PROVIDER = "alpaca_market_data"
ALPACA_NEWS_PROVIDER = "alpaca_news"


@dataclass(frozen=True)
class AlpacaStreamRunResult:
    configured: bool
    connected: bool
    messages_processed: int
    candles_stored: int
    reason: str


@dataclass(frozen=True)
class NewsCollectionResult:
    success: bool
    symbols_seen: int
    feeds_seen: int
    headlines_seen: int
    raw_news_stored: int
    clean_news_stored: int
    duplicates_seen: int
    rumors_seen: int
    reason: str


class AlpacaMarketDataStream:
    """Alpaca websocket market-data stream.

    Official Alpaca docs use websocket auth and subscribe messages. This class
    keeps the stream DB-backed and bounded for agent/test runs via max_messages.
    """

    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_paper_api_key and self.settings.alpaca_paper_secret_key)

    async def run(
        self,
        *,
        symbols: list[str] | None = None,
        channels: list[str] | None = None,
        max_messages: int | None = None,
    ) -> AlpacaStreamRunResult:
        assert_provider_usage(ALPACA_MARKET_DATA_PROVIDER, research=True, intraday=True)
        if not self.configured:
            return AlpacaStreamRunResult(False, False, 0, 0, "Alpaca paper/data keys are not configured.")

        try:
            import websockets
        except ImportError:
            return AlpacaStreamRunResult(False, False, 0, 0, "websockets package is not installed.")

        symbols = symbols or _csv(self.settings.alpaca_stream_symbols)
        channels = channels or _csv(self.settings.alpaca_stream_channels)
        news_only = "news" in channels
        subscribe = {"action": "subscribe"}
        if "trades" in channels and not news_only:
            subscribe["trades"] = symbols
        if "quotes" in channels and not news_only:
            subscribe["quotes"] = symbols
        if "bars" in channels and not news_only:
            subscribe["bars"] = symbols
        if "statuses" in channels and not news_only:
            subscribe["statuses"] = symbols
        if news_only:
            subscribe["news"] = symbols

        processed = candles = 0
        stream_url = (
            self.settings.alpaca_news_stream_url
            if news_only
            else self.settings.alpaca_market_data_stream_url
        )
        try:
            async with websockets.connect(stream_url) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "action": "auth",
                            "key": self.settings.alpaca_paper_api_key,
                            "secret": self.settings.alpaca_paper_secret_key,
                        }
                    )
                )
                await websocket.send(json.dumps(subscribe))
                while max_messages is None or processed < max_messages:
                    message = await websocket.recv()
                    events = json.loads(message)
                    if isinstance(events, dict):
                        events = [events]
                    for event in events:
                        processed += 1
                        if _is_news_event(event):
                            AlpacaNewsStreamCollector(self.repository, self.settings).process_event(event)
                            continue
                        candles += self.process_event(event)
                        if max_messages is not None and processed >= max_messages:
                            break
        except Exception as exc:
            return AlpacaStreamRunResult(True, False, processed, candles, f"Alpaca stream stopped: {exc}")

        return AlpacaStreamRunResult(True, True, processed, candles, "Alpaca stream processed requested messages.")

    def process_event(self, event: dict[str, Any]) -> int:
        event_type = str(event.get("T") or event.get("stream") or "unknown")
        symbol = event.get("S") or event.get("symbol")
        source_timestamp = _parse_alpaca_time(event.get("t") or event.get("timestamp")) or datetime.now(UTC)
        processed = event_type in {"b", "bar"}
        self.repository.store_market_data_stream_event(
            provider=ALPACA_MARKET_DATA_PROVIDER,
            stream_name=self.settings.alpaca_market_data_feed,
            event_type=event_type,
            symbol=symbol,
            source_timestamp=source_timestamp,
            payload=event,
            processed=processed,
            reason="Processed Alpaca websocket event." if processed else "Recorded non-bar stream event.",
        )
        if not processed or not symbol:
            return 0

        open_price = float(event.get("o"))
        high = float(event.get("h"))
        low = float(event.get("l"))
        close = float(event.get("c"))
        volume = float(event.get("v") or 0)
        raw_id = self.repository.store_raw_candle(
            {
                "provider": ALPACA_MARKET_DATA_PROVIDER,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": source_timestamp,
                "raw_payload": event,
            }
        )
        record = MarketDataRecord(
            provider=ALPACA_MARKET_DATA_PROVIDER,
            symbol=symbol,
            timeframe="1Min",
            source_timestamp=source_timestamp,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            trade_count=int(event.get("n") or 0) if event.get("n") is not None else None,
            vwap=float(event.get("vw")) if event.get("vw") is not None else None,
        )
        validation = validate_market_data_record(record, now=source_timestamp)
        self.repository.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": ALPACA_MARKET_DATA_PROVIDER,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": source_timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "trade_count": record.trade_count,
                "vwap": record.vwap,
                "data_quality_status": validation.status.value,
                "quality_reason": validation.reason,
            }
        )
        if validation.status != DataQualityStatus.VALID:
            self.repository.store_data_quality_error(
                provider=ALPACA_MARKET_DATA_PROVIDER,
                symbol=symbol,
                timeframe="1Min",
                data_quality_status=validation.status.value,
                reason=validation.reason,
                source_timestamp=source_timestamp,
                payload=event,
            )
        return 1


class AlpacaNewsStreamCollector:
    """Persist Alpaca news websocket events through the raw/clean news pipeline.

    This collector intentionally has no polling HTTP fallback. The market_stream
    worker can subscribe to the ``news`` channel and process catalyst headlines
    from Alpaca's streaming news feed with the same classifier used downstream.
    """

    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_paper_api_key and self.settings.alpaca_paper_secret_key)

    def collect(self, symbols: list[str] | None = None) -> NewsCollectionResult:
        if not self.configured:
            return NewsCollectionResult(False, 0, 0, 0, 0, 0, 0, 0, "Alpaca news stream keys are not configured.")
        return NewsCollectionResult(
            True,
            len(symbols or self.repository.active_symbols()),
            1,
            0,
            0,
            0,
            0,
            0,
            "News is collected by the Alpaca websocket via the market_stream worker; HTTP polling is disabled.",
        )

    def process_event(self, event: dict[str, Any]) -> NewsCollectionResult:
        headline = str(event.get("headline") or event.get("h") or "").strip()
        if not headline:
            return NewsCollectionResult(True, 0, 1, 0, 0, 0, 0, 0, "Alpaca news event had no headline.")

        symbols = _event_symbols(event)
        source_timestamp = _parse_alpaca_time(event.get("created_at") or event.get("updated_at") or event.get("t")) or datetime.now(UTC)
        raw = self.repository.store_raw_news(
            provider=ALPACA_NEWS_PROVIDER,
            symbol=",".join(symbols) if symbols else None,
            headline=headline,
            url=event.get("url"),
            raw_payload=event,
            source_timestamp=source_timestamp,
        )

        duplicates = rumors = clean = 0
        seen_hashes = self.repository.seen_news_hashes()
        targets = symbols or [None]
        for symbol in targets:
            classification = classify_news_headline(
                headline=headline,
                source=event.get("source") or ALPACA_NEWS_PROVIDER,
                seen_hashes=seen_hashes,
            )
            duplicates += int(classification.duplicate_headline)
            rumors += int(classification.rumor_flag)
            self.repository.store_clean_news(
                raw_news_id=raw.id,
                provider=ALPACA_NEWS_PROVIDER,
                symbol=symbol,
                headline=headline,
                normalized_headline_hash=classification.normalized_headline_hash,
                summary=event.get("summary") or headline,
                source_confidence_score=classification.source_confidence_score,
                duplicate_headline=classification.duplicate_headline,
                rumor_flag=classification.rumor_flag,
                reason=f"{classification.reason} Classifier={classification.classifier_version}. Source=Alpaca news websocket.",
                source_timestamp=source_timestamp,
            )
            clean += 1
            seen_hashes.add(classification.normalized_headline_hash)

        return NewsCollectionResult(True, len(symbols), 1, 1, 1, clean, duplicates, rumors, "Alpaca news websocket event stored.")


def _csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _parse_alpaca_time(value: Any) -> datetime | None:
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


def _is_news_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("T") or event.get("stream") or "").lower()
    return event_type in {"n", "news"} or "headline" in event


def _event_symbols(event: dict[str, Any]) -> list[str]:
    raw = event.get("symbols") or event.get("S") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(symbol).upper() for symbol in raw if symbol]
