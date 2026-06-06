from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


REVIEW_ENGINE_VERSION = "trade_review_v1"


@dataclass(frozen=True)
class ReviewRunResult:
    journal_entries_seen: int
    reviews_created: int
    reason: str
    version: str = REVIEW_ENGINE_VERSION


class TradeReviewEngine:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self) -> ReviewRunResult:
        journals = self.repository.session.scalars(select(models.TradeJournal).limit(500)).all()
        created = 0
        for journal in journals:
            existing = self.repository.session.scalar(
                select(models.AIReview).where(models.AIReview.trade_journal_id == journal.id)
            )
            if existing:
                continue
            review = self._build_review(journal)
            self.repository.store_ai_review(
                trade_journal_id=journal.id,
                prompt_version=REVIEW_ENGINE_VERSION,
                review_text=review,
                confidence_score=70.0,
                reason="Deterministic review generated; AI provider not required.",
                source_timestamp=journal.source_timestamp,
            )
            journal.ai_review = review
            self.repository.session.commit()
            created += 1
        return ReviewRunResult(
            journal_entries_seen=len(journals),
            reviews_created=created,
            reason="Trade reviews generated for unreviewed journal entries.",
        )

    def _build_review(self, journal: models.TradeJournal) -> str:
        pnl_context = "PnL not available yet." if journal.pnl is None else f"PnL recorded: {journal.pnl}."
        duration_context = (
            "Time in trade not available yet."
            if journal.time_in_trade_seconds is None
            else f"Time in trade seconds: {round(journal.time_in_trade_seconds, 2)}."
        )
        mistakes = ", ".join(journal.mistake_tags or []) or "none tagged"
        return (
            f"Review for {journal.symbol} / {journal.strategy_id or 'unknown strategy'}. "
            f"{pnl_context} {duration_context} Mistake tags: {mistakes}. "
            "Verify that entry, exit, risk, catalyst, and regime rules were followed."
        )
