from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from trading_system.app.core.config import Settings, get_settings

TRADING_EVENTS_CHANNEL = "trading:events:v1"


@dataclass(frozen=True)
class TradingStreamEvent:
    type: str
    payload: dict[str, Any]
    source: str
    published_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
        }


def publish_trading_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    source: str,
    settings: Settings | None = None,
) -> bool:
    """Publish a dashboard event to Redis pub/sub.

    Returns False when Redis is unavailable so market-data ingestion never fails just
    because the real-time dashboard bridge is offline.
    """
    settings = settings or get_settings()
    import redis

    event = TradingStreamEvent(
        type=event_type,
        payload=payload,
        source=source,
        published_at=datetime.now(UTC),
    )
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        client.publish(TRADING_EVENTS_CHANNEL, json.dumps(event.as_dict(), default=str))
        return True
    except redis.RedisError:
        return False


async def redis_event_stream(
    *,
    settings: Settings | None = None,
    channel: str = TRADING_EVENTS_CHANNEL,
) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded Redis pub/sub events for the FastAPI WebSocket gateway."""
    settings = settings or get_settings()
    import redis

    client = redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel)
    try:
        while True:
            message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
            if not message:
                await asyncio.sleep(0.05)
                continue
            data = message.get("data")
            if not isinstance(data, str):
                continue
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                yield {
                    "type": "STREAM_DECODE_ERROR",
                    "payload": {"raw": data},
                    "source": "redis_event_stream",
                    "published_at": datetime.now(UTC).isoformat(),
                }
    finally:
        await asyncio.to_thread(pubsub.close)
        await asyncio.to_thread(client.close)
