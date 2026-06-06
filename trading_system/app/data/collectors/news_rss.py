from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from trading_system.app.catalysts.news_classifier import classify_news_headline
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.db.repositories import TradingRepository


NEWS_PROVIDER = "news"


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


class NewsRssCollector:
    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.http = http or requests.Session()

    def collect(self, symbols: list[str] | None = None) -> NewsCollectionResult:
        assert_provider_usage(NEWS_PROVIDER, research=True, enrichment=True)
        symbols = [symbol.upper() for symbol in (symbols or self.repository.active_symbols())]
        feeds = _csv(self.settings.news_rss_feeds)
        if not feeds:
            return NewsCollectionResult(False, len(symbols), 0, 0, 0, 0, 0, 0, "No RSS feeds configured.")

        seen_hashes = self.repository.seen_news_hashes()
        headlines_seen = raw_stored = clean_stored = duplicates = rumors = feeds_seen = 0
        errors: list[str] = []

        for symbol in symbols:
            for template in feeds:
                url = template.format(symbol=symbol)
                started = time.monotonic()
                try:
                    response = self.http.get(url, timeout=15)
                    duration_ms = (time.monotonic() - started) * 1000
                    self.repository.log_api_call(
                        provider=NEWS_PROVIDER,
                        endpoint=url,
                        status_code=response.status_code,
                        success=response.ok,
                        reason="RSS feed fetched." if response.ok else "RSS feed returned non-OK status.",
                        duration_ms=duration_ms,
                    )
                    response.raise_for_status()
                    items = _parse_rss_items(response.text)
                    feeds_seen += 1
                except (requests.RequestException, ET.ParseError) as exc:
                    errors.append(f"{symbol}: {exc}")
                    self.repository.log_api_call(
                        provider=NEWS_PROVIDER,
                        endpoint=url,
                        status_code=getattr(getattr(exc, "response", None), "status_code", None),
                        success=False,
                        reason=f"RSS feed collection failed: {exc}",
                    )
                    continue

                for item in items:
                    headline = item.get("headline", "").strip()
                    if not headline:
                        continue
                    headlines_seen += 1
                    source_timestamp = _parse_rss_time(item.get("published_at")) or datetime.now(UTC)
                    raw = self.repository.store_raw_news(
                        provider=NEWS_PROVIDER,
                        symbol=symbol,
                        headline=headline,
                        url=item.get("url"),
                        raw_payload=item,
                        source_timestamp=source_timestamp,
                    )
                    raw_stored += 1
                    classification = classify_news_headline(
                        headline=headline,
                        source=item.get("source") or item.get("url") or NEWS_PROVIDER,
                        seen_hashes=seen_hashes,
                    )
                    if classification.duplicate_headline:
                        duplicates += 1
                    if classification.rumor_flag:
                        rumors += 1
                    clean = self.repository.store_clean_news(
                        raw_news_id=raw.id,
                        provider=NEWS_PROVIDER,
                        symbol=symbol,
                        headline=headline,
                        normalized_headline_hash=classification.normalized_headline_hash,
                        summary=item.get("summary") or headline,
                        source_confidence_score=classification.source_confidence_score,
                        duplicate_headline=classification.duplicate_headline,
                        rumor_flag=classification.rumor_flag,
                        reason=f"{classification.reason} Classifier={classification.classifier_version}.",
                        source_timestamp=source_timestamp,
                    )
                    clean_stored += 1
                    seen_hashes.add(clean.normalized_headline_hash)

        success = not errors or raw_stored > 0
        reason = "News RSS collection completed."
        if errors:
            reason += f" Errors: {'; '.join(errors[:3])}"
        return NewsCollectionResult(
            success=success,
            symbols_seen=len(symbols),
            feeds_seen=feeds_seen,
            headlines_seen=headlines_seen,
            raw_news_stored=raw_stored,
            clean_news_stored=clean_stored,
            duplicates_seen=duplicates,
            rumors_seen=rumors,
            reason=reason,
        )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_rss_items(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _find_text(item, "title")
        link = _find_text(item, "link")
        description = _find_text(item, "description")
        pub_date = _find_text(item, "pubDate")
        source = _find_text(item, "source")
        items.append(
            {
                "headline": title,
                "url": link,
                "summary": description,
                "published_at": pub_date,
                "source": source,
            }
        )
    return items


def _find_text(item: ET.Element, tag: str) -> str | None:
    element = item.find(tag)
    if element is not None and element.text:
        return element.text.strip()
    for child in item:
        if child.tag.endswith(tag) and child.text:
            return child.text.strip()
    return None


def _parse_rss_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
