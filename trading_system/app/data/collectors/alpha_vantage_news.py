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
# Alpha Vantage caps NEWS_SENTIMENT at 1000 articles per request.
_MAX_FEED_LIMIT = 1000


class AlphaVantageNewsCollector:
    """Collect market-wide news from Alpha Vantage's NEWS_SENTIMENT endpoint.

    News-only mode scans the entire US stock & ETF universe (~13k symbols), which
    makes a per-symbol request impossible on the free tier (~25 requests/day).
    Instead this collector issues a single *broad* request with **no** ``tickers``
    filter (``sort=LATEST``, ``limit`` up to 1000) and fans the result out into one
    ``CleanNews`` row per ``ticker_sentiment`` entry, keeping only tickers that are
    in the active universe. Each kept row also stores the Alpha Vantage per-ticker
    ``sentiment_score`` and ``relevance_score`` so the news screener can rank on
    real signal. Articles are persisted through the same raw/clean news pipeline
    used by the RSS collector so downstream catalyst logic is unchanged.

    NOTE: a no-``tickers`` broad pull is *not* the multi-ticker AND filter that the
    Alpha Vantage docs warn about (``tickers=A,B,C`` only returns articles that
    mention every listed ticker). The broad feed returns the latest market news
    across all symbols, which is exactly what universe-wide coverage needs.
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
        active = {symbol.upper() for symbol in (symbols or self.repository.active_symbols())}
        api_key = self.settings.alpha_vantage_api_key.strip()
        if not api_key:
            return NewsCollectionResult(
                False, len(active), 0, 0, 0, 0, 0, 0, "Alpha Vantage API key not configured."
            )
        if not active:
            return NewsCollectionResult(
                True, 0, 0, 0, 0, 0, 0, 0, "No active symbols to collect news for."
            )

        limit = min(_MAX_FEED_LIMIT, max(1, self.settings.alpha_vantage_news_limit))
        params = {
            "function": "NEWS_SENTIMENT",
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
            self.repository.log_api_call(
                provider=NEWS_PROVIDER,
                endpoint=ALPHA_VANTAGE_NEWS_URL,
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                success=False,
                reason=f"Alpha Vantage news collection failed: {message}",
            )
            return NewsCollectionResult(
                False,
                len(active),
                0,
                0,
                0,
                0,
                0,
                0,
                f"Alpha Vantage news collection failed: {message}",
            )

        feed = payload.get("feed") if isinstance(payload, dict) else None
        if not isinstance(feed, list):
            note = "No feed returned."
            if isinstance(payload, dict):
                note = (
                    payload.get("Note")
                    or payload.get("Information")
                    or payload.get("Error Message")
                    or "No feed returned."
                )
            return NewsCollectionResult(
                False,
                len(active),
                0,
                0,
                0,
                0,
                0,
                0,
                f"Alpha Vantage news collection failed: {_redact(note, api_key)}",
            )

        # Duplicate detection is per-symbol: the same headline can legitimately
        # appear once for each ticker it mentions, but a repeat for the *same*
        # ticker is a duplicate.
        seen_by_symbol: dict[str, set[str]] = {}
        headlines_seen = raw_stored = clean_stored = duplicates = rumors = 0

        for article in feed:
            if not isinstance(article, dict):
                continue
            headline = (article.get("title") or "").strip()
            if not headline:
                continue
            headlines_seen += 1

            matched = _matched_tickers(article.get("ticker_sentiment"), active)
            if not matched:
                continue

            url = article.get("url")
            summary = (article.get("summary") or headline).strip()
            source = article.get("source") or article.get("source_domain") or NEWS_PROVIDER
            source_timestamp = _parse_av_time(article.get("time_published")) or datetime.now(UTC)

            # One raw row per article; clean rows fan out per matched ticker.
            raw = self.repository.store_raw_news(
                provider=NEWS_PROVIDER,
                symbol=None,
                headline=headline,
                url=url,
                raw_payload=article,
                source_timestamp=source_timestamp,
            )
            raw_stored += 1

            for symbol, sentiment_score, relevance_score in matched:
                seen = seen_by_symbol.setdefault(symbol, set())
                classification = classify_news_headline(
                    headline=headline,
                    source=source,
                    seen_hashes=seen,
                )
                if classification.duplicate_headline:
                    duplicates += 1
                if classification.rumor_flag:
                    rumors += 1
                self.repository.store_clean_news(
                    raw_news_id=raw.id,
                    provider=NEWS_PROVIDER,
                    symbol=symbol,
                    headline=headline,
                    normalized_headline_hash=classification.normalized_headline_hash,
                    summary=summary,
                    source_confidence_score=classification.source_confidence_score,
                    duplicate_headline=classification.duplicate_headline,
                    rumor_flag=classification.rumor_flag,
                    sentiment_score=sentiment_score,
                    relevance_score=relevance_score,
                    reason=(
                        f"{classification.reason} Classifier={classification.classifier_version}. "
                        "Source=Alpha Vantage broad feed."
                    ),
                    source_timestamp=source_timestamp,
                )
                clean_stored += 1
                seen.add(classification.normalized_headline_hash)

        reason = (
            f"Alpha Vantage broad news feed processed {headlines_seen} articles into "
            f"{clean_stored} universe-matched rows."
        )
        return NewsCollectionResult(
            success=True,
            symbols_seen=len(active),
            feeds_seen=1,
            headlines_seen=headlines_seen,
            raw_news_stored=raw_stored,
            clean_news_stored=clean_stored,
            duplicates_seen=duplicates,
            rumors_seen=rumors,
            reason=reason,
        )


def _matched_tickers(
    ticker_sentiment: object,
    active: set[str],
) -> list[tuple[str, float | None, float | None]]:
    """Return ``(symbol, sentiment_score, relevance_score)`` for each
    ``ticker_sentiment`` entry whose ticker is in the active universe."""

    if not isinstance(ticker_sentiment, list):
        return []
    matched: list[tuple[str, float | None, float | None]] = []
    seen: set[str] = set()
    for entry in ticker_sentiment:
        if not isinstance(entry, dict):
            continue
        ticker = (entry.get("ticker") or "").strip().upper()
        if not ticker or ticker not in active or ticker in seen:
            continue
        seen.add(ticker)
        matched.append(
            (
                ticker,
                _to_float(entry.get("ticker_sentiment_score")),
                _to_float(entry.get("relevance_score")),
            )
        )
    return matched


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
