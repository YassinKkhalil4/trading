from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import DataQualityStatus
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.data.validators.market_data import MarketDataRecord, validate_market_data_record
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.streaming_events import publish_trading_event


ALPACA_MARKET_DATA_PROVIDER = "alpaca_market_data"


@dataclass(frozen=True)
class AlpacaStreamRunResult:
    configured: bool
    connected: bool
    messages_processed: int
    candles_stored: int
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
        subscribe = {"action": "subscribe"}
        if "trades" in channels:
            subscribe["trades"] = symbols
        if "quotes" in channels:
            subscribe["quotes"] = symbols
        if "bars" in channels:
            subscribe["bars"] = symbols
        if "statuses" in channels:
            subscribe["statuses"] = symbols

        processed = candles = 0
        attempts = 0
        last_heartbeat_at: datetime | None = None
        while True:
            connected_at: datetime | None = None
            try:
                async with websockets.connect(self.settings.alpaca_market_data_stream_url) as websocket:
                    connected_at = datetime.now(UTC)
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
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                        except TimeoutError as exc:
                            await websocket.close()
                            raise ConnectionError("No Alpaca stream messages received for 30 seconds.") from exc

                        events = json.loads(message)
                        if isinstance(events, dict):
                            events = [events]
                        batch_candles = 0
                        for event in events:
                            processed += 1
                            batch_candles += self.process_event(event)
                            if max_messages is not None and processed >= max_messages:
                                break
                        candles += batch_candles
                        last_heartbeat_at = self._heartbeat_if_due(
                            last_heartbeat_at=last_heartbeat_at,
                            processed=processed,
                            candles=candles,
                        )
                    return AlpacaStreamRunResult(
                        True, True, processed, candles, "Alpaca stream processed requested messages."
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                held_for_seconds = (datetime.now(UTC) - connected_at).total_seconds() if connected_at else 0.0
                attempts = 0 if held_for_seconds > 60 else attempts + 1
                if max_messages is not None and processed >= max_messages:
                    return AlpacaStreamRunResult(
                        True, True, processed, candles, "Alpaca stream processed requested messages."
                    )
                await asyncio.sleep(min(2**attempts, 60))
                if max_messages is not None:
                    return AlpacaStreamRunResult(True, False, processed, candles, f"Alpaca stream stopped: {exc}")

    def _heartbeat_if_due(
        self,
        *,
        last_heartbeat_at: datetime | None,
        processed: int,
        candles: int,
    ) -> datetime:
        now = datetime.now(UTC)
        if last_heartbeat_at and (now - last_heartbeat_at).total_seconds() < 5:
            return last_heartbeat_at
        self.repository.store_worker_heartbeat(
            worker_name="alpaca_stream",
            status="HEALTHY",
            last_started_at=None,
            last_finished_at=now,
            last_success=True,
            reason="Alpaca stream processed market data batch.",
            payload={"messages_processed": processed, "candles_stored": candles},
        )
        return now

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
        publish_trading_event(
            "MARKET_DATA_STREAM_EVENT",
            {
                "provider": ALPACA_MARKET_DATA_PROVIDER,
                "stream_name": self.settings.alpaca_market_data_feed,
                "event_type": event_type,
                "symbol": symbol,
                "source_timestamp": source_timestamp.isoformat(),
                "processed": processed,
                "payload": event,
            },
            source="alpaca_stream",
            settings=self.settings,
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
        publish_trading_event(
            "MARKET_DATA_CANDLE",
            {
                "provider": ALPACA_MARKET_DATA_PROVIDER,
                "symbol": symbol,
                "timeframe": "1Min",
                "timestamp": source_timestamp.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "vwap": record.vwap,
                "data_quality_status": validation.status.value,
            },
            source="alpaca_stream",
            settings=self.settings,
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

