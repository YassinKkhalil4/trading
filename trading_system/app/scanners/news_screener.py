from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, cast, func, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


NEWS_SCREENER_NAME = "NEWS_SENTIMENT_SCREENER"
NEWS_SCREENER_VERSION = "news_screener_v1"
_DEFAULT_LOOKBACK_HOURS = 48
_DEFAULT_TOP_N = 50


@dataclass(frozen=True)
class NewsScreenerRunResult:
    symbols_scored: int
    results_stored: int
    reason: str
    lookback_hours: int
    version: str = NEWS_SCREENER_VERSION
    top_symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SymbolNews:
    symbol: str
    news_count: int
    avg_sentiment: float
    avg_relevance: float
    avg_confidence: float
    rumor_count: int
    duplicate_count: int

    @property
    def rumor_ratio(self) -> float:
        return self.rumor_count / self.news_count if self.news_count else 0.0

    @property
    def duplicate_ratio(self) -> float:
        return self.duplicate_count / self.news_count if self.news_count else 0.0

    @property
    def direction(self) -> str:
        if self.avg_sentiment > 0.05:
            return "bullish"
        if self.avg_sentiment < -0.05:
            return "bearish"
        return "neutral"

    @property
    def score(self) -> float:
        """Rank opportunities by coverage weighted by relevance, source
        confidence and sentiment magnitude, penalised for unverified rumors.

        The score is always >= 0; sentiment can only amplify (positive) or
        dampen (negative) the coverage signal, never make it negative.
        """

        confidence = max(0.0, min(self.avg_confidence, 100.0)) / 100.0
        relevance = max(0.0, min(self.avg_relevance, 1.0))
        sentiment = max(-1.0, min(self.avg_sentiment, 1.0))
        return (
            self.news_count
            * (0.5 + 0.5 * relevance)
            * (0.4 + 0.6 * confidence)
            * (1.0 + 0.5 * sentiment)
            * (1.0 - 0.5 * self.rumor_ratio)
        )


class NewsOpportunityScanner:
    """News-only opportunity screener.

    Aggregates recently ingested ``CleanNews`` per symbol and persists the
    top-ranked symbols into the existing ``scanner_results`` table under
    ``scanner_name="NEWS_SENTIMENT_SCREENER"``. This deliberately writes no
    ``TradeSignal`` rows — news-only mode surfaces opportunities for review, it
    does not place trades.
    """

    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings | None = None,
        *,
        lookback_hours: int | None = None,
        top_n: int | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.lookback_hours = lookback_hours or _DEFAULT_LOOKBACK_HOURS
        self.top_n = top_n or _DEFAULT_TOP_N

    def run_once(self) -> NewsScreenerRunResult:
        cutoff = datetime.now(UTC) - timedelta(hours=self.lookback_hours)
        ranked = sorted(
            self._aggregate(cutoff),
            key=lambda item: item.score,
            reverse=True,
        )
        selected = ranked[: self.top_n]
        now = datetime.now(UTC)
        for entry in selected:
            self.repository.store_generic_scanner_result(
                scanner_name=NEWS_SCREENER_NAME,
                scanner_rule_version=NEWS_SCREENER_VERSION,
                symbol=entry.symbol,
                strategy_id=NEWS_SCREENER_NAME,
                accepted=True,
                score=round(entry.score, 4),
                reason=(
                    f"{entry.news_count} article(s) in last {self.lookback_hours}h; "
                    f"sentiment {entry.avg_sentiment:+.2f} ({entry.direction}), "
                    f"relevance {entry.avg_relevance:.2f}, "
                    f"confidence {entry.avg_confidence:.0f}, "
                    f"rumor ratio {entry.rumor_ratio:.0%}."
                ),
                payload={
                    "news_count": entry.news_count,
                    "avg_sentiment": round(entry.avg_sentiment, 4),
                    "avg_relevance": round(entry.avg_relevance, 4),
                    "avg_confidence": round(entry.avg_confidence, 2),
                    "rumor_count": entry.rumor_count,
                    "duplicate_count": entry.duplicate_count,
                    "rumor_ratio": round(entry.rumor_ratio, 4),
                    "duplicate_ratio": round(entry.duplicate_ratio, 4),
                    "direction": entry.direction,
                    "lookback_hours": self.lookback_hours,
                },
                source_timestamp=now,
            )
        reason = (
            f"News screener scored {len(ranked)} symbol(s) from the last "
            f"{self.lookback_hours}h and stored the top {len(selected)}."
        )
        return NewsScreenerRunResult(
            symbols_scored=len(ranked),
            results_stored=len(selected),
            reason=reason,
            lookback_hours=self.lookback_hours,
            top_symbols=[entry.symbol for entry in selected],
        )

    def _aggregate(self, cutoff: datetime) -> list[_SymbolNews]:
        active_subquery = (
            select(models.SymbolUniverse.symbol)
            .where(models.SymbolUniverse.is_active.is_(True))
            .scalar_subquery()
        )
        rows = self.repository.session.execute(
            select(
                models.CleanNews.symbol,
                func.count(models.CleanNews.id),
                func.avg(models.CleanNews.sentiment_score),
                func.avg(models.CleanNews.relevance_score),
                func.avg(models.CleanNews.source_confidence_score),
                func.sum(cast(models.CleanNews.rumor_flag, Integer)),
                func.sum(cast(models.CleanNews.duplicate_headline, Integer)),
            )
            .where(
                models.CleanNews.symbol.is_not(None),
                models.CleanNews.symbol.in_(active_subquery),
                models.CleanNews.created_at >= cutoff,
            )
            .group_by(models.CleanNews.symbol)
        ).all()

        aggregates: list[_SymbolNews] = []
        for symbol, count, sentiment, relevance, confidence, rumors, duplicates in rows:
            if not symbol or not count:
                continue
            aggregates.append(
                _SymbolNews(
                    symbol=symbol,
                    news_count=int(count),
                    avg_sentiment=float(sentiment) if sentiment is not None else 0.0,
                    avg_relevance=float(relevance) if relevance is not None else 0.0,
                    avg_confidence=float(confidence) if confidence is not None else 0.0,
                    rumor_count=int(rumors or 0),
                    duplicate_count=int(duplicates or 0),
                )
            )
        return aggregates
