from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, select

from trading_system.app.core.enums import CatalystDirection
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


CATALYST_ENGINE_VERSION = "catalyst_engine_v1"


@dataclass(frozen=True)
class CatalystRunResult:
    news_seen: int
    filings_seen: int
    events_created: int
    catalysts_created: int
    reason: str
    version: str = CATALYST_ENGINE_VERSION


class CatalystEngine:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self, symbols: list[str] | None = None) -> CatalystRunResult:
        symbols = [item.upper() for item in (symbols or self.repository.active_symbols())]
        news_rows = self.repository.session.scalars(
            select(models.CleanNews)
            .where(models.CleanNews.symbol.in_(symbols))
            .order_by(desc(models.CleanNews.created_at))
            .limit(200)
        ).all()
        filing_rows = self.repository.session.scalars(
            select(models.FilingEvent)
            .where(models.FilingEvent.symbol.in_(symbols))
            .order_by(desc(models.FilingEvent.created_at))
            .limit(200)
        ).all()
        events = catalysts = 0
        now = datetime.now(UTC)
        for news in news_rows:
            taxonomy = classify_news_catalyst_taxonomy(
                news.headline,
                source_timestamp=news.source_timestamp,
                now=now,
            )
            catalyst_type = str(taxonomy["catalyst_type"])
            direction = CatalystDirection(str(taxonomy["direction"]))
            materiality = float(taxonomy["materiality_score"])
            score = max(0.0, materiality * (news.source_confidence_score / 100.0))
            self.repository.store_news_catalyst_score(
                clean_news_id=news.id,
                symbol=news.symbol,
                catalyst_type=catalyst_type,
                source_confidence_score=news.source_confidence_score,
                materiality_score=score,
                rumor_flag=news.rumor_flag,
                duplicate_headline=news.duplicate_headline,
                taxonomy=taxonomy,
                reason=str(taxonomy["reason"]),
                source_timestamp=news.source_timestamp,
            )
            event = self.repository.store_event(
                symbol=news.symbol,
                event_type=catalyst_type,
                event_time=news.source_timestamp,
                summary=news.summary or news.headline,
                direction=direction.value,
                materiality_score=score,
                time_horizon="1_day_to_2_weeks",
                confidence=news.source_confidence_score,
                source=news.provider,
                reason=str(taxonomy["reason"]),
                source_timestamp=news.source_timestamp,
            )
            events += 1
            if news.symbol:
                self.repository.store_catalyst(
                    event_id=event.id,
                    symbol=news.symbol,
                    catalyst_type=catalyst_type,
                    direction=direction.value,
                    materiality_score=score,
                    confidence=news.source_confidence_score,
                    source=news.provider,
                    reason=str(taxonomy["reason"]),
                    source_timestamp=news.source_timestamp,
                )
                catalysts += 1

        for filing in filing_rows:
            catalyst_type, direction, materiality = classify_filing_catalyst(filing.form_type)
            event = self.repository.store_event(
                symbol=filing.symbol,
                event_type=catalyst_type,
                event_time=filing.source_timestamp,
                summary=filing.summary or f"{filing.symbol} SEC filing {filing.form_type}",
                direction=direction.value,
                materiality_score=max(materiality, filing.materiality_score or 0.0),
                time_horizon="1_day_to_6_weeks",
                confidence=85.0,
                source="sec_edgar",
                reason="SEC filing converted into normalized event.",
                source_timestamp=filing.source_timestamp,
            )
            events += 1
            if filing.symbol:
                self.repository.store_catalyst(
                    event_id=event.id,
                    symbol=filing.symbol,
                    catalyst_type=catalyst_type,
                    direction=direction.value,
                    materiality_score=max(materiality, filing.materiality_score or 0.0),
                    confidence=85.0,
                    source="sec_edgar",
                    reason="SEC event converted into catalyst.",
                    source_timestamp=filing.source_timestamp,
                )
                catalysts += 1

        return CatalystRunResult(
            news_seen=len(news_rows),
            filings_seen=len(filing_rows),
            events_created=events,
            catalysts_created=catalysts,
            reason="Catalyst engine scored available news and SEC filing context.",
        )


def classify_news_catalyst(headline: str) -> tuple[str, CatalystDirection, float]:
    taxonomy = classify_news_catalyst_taxonomy(headline)
    return (
        str(taxonomy["catalyst_type"]),
        CatalystDirection(str(taxonomy["direction"])),
        float(taxonomy["materiality_score"]),
    )


def classify_news_catalyst_taxonomy(
    headline: str,
    *,
    source_timestamp: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    text = headline.lower()
    bullish_terms = ["beat", "raise", "upgrade", "approval", "partnership", "wins", "launch"]
    bearish_terms = ["miss", "cut", "downgrade", "lawsuit", "probe", "recall", "delay"]
    if any(term in text for term in bullish_terms):
        catalyst_type, direction, base = "news_momentum", CatalystDirection.BULLISH, 70.0
    elif any(term in text for term in bearish_terms):
        catalyst_type, direction, base = "news_risk", CatalystDirection.BEARISH, 70.0
    else:
        catalyst_type, direction, base = "news_context", CatalystDirection.NEUTRAL, 35.0
    freshness_multiplier = _freshness_multiplier(source_timestamp, now)
    materiality = round(base * freshness_multiplier, 2)
    return {
        "catalyst_type": catalyst_type,
        "direction": direction.value,
        "base_materiality_score": base,
        "materiality_score": materiality,
        "freshness_multiplier": freshness_multiplier,
        "engine_version": CATALYST_ENGINE_VERSION,
        "reason": (
            f"Deterministic catalyst taxonomy assigned {catalyst_type}; "
            f"freshness multiplier {freshness_multiplier:.2f}."
        ),
    }


def _freshness_multiplier(source_timestamp: datetime | None, now: datetime | None = None) -> float:
    if source_timestamp is None:
        return 0.75
    current = now or datetime.now(UTC)
    timestamp = source_timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age_hours = max(0.0, (current - timestamp.astimezone(UTC)).total_seconds() / 3600.0)
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.75
    if age_hours <= 168:
        return 0.5
    return 0.25


def classify_filing_catalyst(form_type: str | None) -> tuple[str, CatalystDirection, float]:
    form = (form_type or "").upper()
    if form in {"8-K", "6-K"}:
        return "material_filing", CatalystDirection.NEUTRAL, 75.0
    if form in {"10-Q", "10-K", "20-F"}:
        return "earnings_or_fundamental_filing", CatalystDirection.NEUTRAL, 65.0
    if form in {"S-1", "S-3", "424B", "424B5"}:
        return "capital_markets_filing", CatalystDirection.NEUTRAL, 60.0
    if form in {"3", "4", "5"}:
        return "insider_transaction", CatalystDirection.NEUTRAL, 45.0
    return "filing_context", CatalystDirection.NEUTRAL, 30.0
