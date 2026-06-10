from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import requests

from trading_system.app.catalysts.news_classifier import classify_news_headline
from trading_system.app.core.config import Settings, get_settings
from trading_system.app.data.collectors.news_rss import NEWS_PROVIDER, NewsCollectionResult
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.db.repositories import TradingRepository


ALPHA_VANTAGE_NEWS_URL = "https://www.alphavantage.co/query"
_APIKEY_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


class AlphaVantageNewsCollector:
    """Collect market news from Alpha Vantage's NEWS_SENTIMENT endpoint.

    One request is issued per symbol because Alpha Vantage treats multiple
    comma-separated tickers as an AND filter (it only returns articles that
    mention every listed ticker), which is not what per-symbol catalyst
    enrichment needs. Articles are persisted through the same raw/clean news
    pipeline used by the RSS collector so all downstream catalyst logic is
    unchanged.
    """

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
        api_key = self.settings.alpha_vantage_api_key.strip()
        if not api_key:
            return NewsCollectionResult(
                False, len(symbols), 0, 0, 0, 0, 0, 0, "Alpha Vantage API key not configured."
            )
        if not symbols:
            return NewsCollectionResult(
                True, 0, 0, 0, 0, 0, 0, 0, "No active symbols to collect news for."
            )

        limit = max(1, self.settings.alpha_vantage_news_limit)
        seen_hashes = self.repository.seen_news_hashes()
        feeds_seen = headlines_seen = raw_stored = clean_stored = duplicates = rumors = 0
        errors: list[str] = []

        for symbol in symbols:
            params = {
                "function": "NEWS_SENTIMENT",
                "tickers": symbol,
                "sort": "LATEST",
                "limit": str(limit),
                "apikey": api_key,
            }
            started = time.monotonic()
            try:
                response = self.http.get(ALPHA_VANTAGE_NEWS_URL, params=params, timeout=20)
                duration_ms = (time.monotonic() - started) * 1000
                self.repository.log_api_call(
                    provider=NEWS_PROVIDER,
                    endpoint=ALPHA_VANTAGE_NEWS_URL,
                    status_code=response.status_code,
                    success=response.ok,
                    reason=(
                        "Alpha Vantage news fetched."
                        if response.ok
                        else "Alpha Vantage returned non-OK status."
                    ),
                    duration_ms=duration_ms,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                message = _redact(exc, api_key)
                errors.append(f"{symbol}: {message}")
                self.repository.log_api_call(
                    provider=NEWS_PROVIDER,
                    endpoint=ALPHA_VANTAGE_NEWS_URL,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                    success=False,
                    reason=f"Alpha Vantage news collection failed: {message}",
                )
                continue

            feed = payload.get("feed") if isinstance(payload, dict) else None
            if not isinstance(feed, list):
                note = ""
                if isinstance(payload, dict):
                    note = (
                        payload.get("Note")
                        or payload.get("Information")
                        or payload.get("Error Message")
                        or "No feed returned."
                    )
                errors.append(f"{symbol}: {_redact(note, api_key)}")
                continue
            feeds_seen += 1

            for article in feed:
                if not isinstance(article, dict):
                    continue
                headline = (article.get("title") or "").strip()
                if not headline:
                    continue
                url = article.get("url")
                summary = (article.get("summary") or headline).strip()
                source = article.get("source") or article.get("source_domain") or NEWS_PROVIDER
                source_timestamp = _parse_av_time(article.get("time_published")) or datetime.now(UTC)

                headlines_seen += 1
                raw = self.repository.store_raw_news(
                    provider=NEWS_PROVIDER,
                    symbol=symbol,
                    headline=headline,
                    url=url,
                    raw_payload=article,
                    source_timestamp=source_timestamp,
                )
                raw_stored += 1
                classification = classify_news_headline(
                    headline=headline,
                    source=source,
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
                    summary=summary,
                    source_confidence_score=classification.source_confidence_score,
                    duplicate_headline=classification.duplicate_headline,
                    rumor_flag=classification.rumor_flag,
                    reason=(
                        f"{classification.reason} Classifier={classification.classifier_version}. "
                        "Source=Alpha Vantage."
                    ),
                    source_timestamp=source_timestamp,
                )
                clean_stored += 1
                seen_hashes.add(clean.normalized_headline_hash)

        success = (not errors) or raw_stored > 0
        reason = "Alpha Vantage news collection completed."
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


def _redact(value: object, api_key: str) -> str:
    """Strip the Alpha Vantage API key from any text before it is logged.

    ``requests`` exception messages embed the full request URL (including the
    ``apikey`` query parameter), so anything derived from an exception must be
    sanitized before it lands in persisted logs or the dashboard.
    """

    cleaned = _APIKEY_RE.sub(r"\1***", str(value))
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
    return cleaned


def _parse_av_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC)
