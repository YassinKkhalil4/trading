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
        if symbols is None:
            # Scheduler path: the active universe can be ~13k symbols, which would
            # blow up a literal IN(...) clause. The news collector already filters
            # stored news to the active universe, so scope by a subquery instead of
            # materializing every symbol as a bind parameter.
            active_symbols = (
                select(models.SymbolUniverse.symbol)
                .where(models.SymbolUniverse.is_active.is_(True))
                .scalar_subquery()
            )
            news_filter = models.CleanNews.symbol.in_(active_symbols)
            filing_filter = models.FilingEvent.symbol.in_(active_symbols)
        else:
            symbol_list = [item.upper() for item in symbols]
            news_filter = models.CleanNews.symbol.in_(symbol_list)
            filing_filter = models.FilingEvent.symbol.in_(symbol_list)
        news_rows = self.repository.session.scalars(
            select(models.CleanNews)
            .where(models.CleanNews.symbol.is_not(None))
            .where(news_filter)
            .order_by(desc(models.CleanNews.created_at))
            .limit(200)
        ).all()
        filing_rows = self.repository.session.scalars(
            select(models.FilingEvent)
            .where(models.FilingEvent.symbol.is_not(None))
            .where(filing_filter)
            .order_by(desc(models.FilingEvent.created_at))
            .limit(200)
        ).all()
        events = catalysts = 0
        for news in news_rows:
            catalyst_type, direction, materiality = classify_news_catalyst(news.headline)
            score = max(0.0, materiality * (news.source_confidence_score / 100.0))
            self.repository.store_news_catalyst_score(
                clean_news_id=news.id,
                symbol=news.symbol,
                catalyst_type=catalyst_type,
                source_confidence_score=news.source_confidence_score,
                materiality_score=score,
                rumor_flag=news.rumor_flag,
                duplicate_headline=news.duplicate_headline,
                reason="News converted to catalyst score.",
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
                reason="Clean news converted into normalized event.",
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
                    reason="News event converted into catalyst.",
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
    text = headline.lower()
    bullish_terms = ["beat", "raise", "upgrade", "approval", "partnership", "wins", "launch"]
    bearish_terms = ["miss", "cut", "downgrade", "lawsuit", "probe", "recall", "delay"]
    if any(term in text for term in bullish_terms):
        return "news_momentum", CatalystDirection.BULLISH, 70.0
    if any(term in text for term in bearish_terms):
        return "news_risk", CatalystDirection.BEARISH, 70.0
    return "news_context", CatalystDirection.NEUTRAL, 35.0


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
