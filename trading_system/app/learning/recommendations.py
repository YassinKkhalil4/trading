from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


LEARNING_ENGINE_VERSION = "learning_recommendations_v1"


@dataclass(frozen=True)
class LearningRunResult:
    weekly_review_id: str
    recommendations_created: int
    reason: str
    version: str = LEARNING_ENGINE_VERSION


class LearningRecommendationEngine:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = LearningJournalRepository(repository)

    def run_weekly_review(self) -> LearningRunResult:
        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)
        metrics = self._metrics(week_start, now)
        summary = (
            f"Weekly review: {metrics['journal_entries']} journal entries, "
            f"{metrics['winning_trades']} winners, {metrics['losing_trades']} losers, "
            f"total PnL {metrics['total_pnl']}."
        )
        review = self.repository.store_weekly_review(
            week_start=week_start,
            week_end=now,
            summary=summary,
            metrics=metrics,
            reason="Automated weekly review generated recommendations only.",
        )
        recommendations = self._recommend(metrics)
        for item in recommendations:
            self.repository.store_strategy_recommendation(
                strategy_id=item["strategy_id"],
                recommendation=item["recommendation"],
                reason=item["reason"],
            )
        return LearningRunResult(
            weekly_review_id=review.id,
            recommendations_created=len(recommendations),
            reason="Learning layer generated non-mutating recommendations.",
        )

    def _metrics(self, start: datetime, end: datetime) -> dict:
        entries = self.repository.journal_entries_between(start, end)
        pnls = [float(entry.pnl) for entry in entries if entry.pnl is not None]
        slippage_values = [
            float(entry.slippage_bps) for entry in entries if entry.slippage_bps is not None
        ]
        hold_times = [
            float(entry.time_in_trade_seconds)
            for entry in entries
            if entry.time_in_trade_seconds is not None
        ]
        rule_violation_count = sum(1 for entry in entries if entry.rule_violations)
        return {
            "journal_entries": len(entries),
            "winning_trades": sum(1 for pnl in pnls if pnl > 0),
            "losing_trades": sum(1 for pnl in pnls if pnl < 0),
            "total_pnl": sum(pnls),
            "average_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "average_slippage_bps": (
                sum(slippage_values) / len(slippage_values) if slippage_values else 0.0
            ),
            "average_time_in_trade_seconds": (
                sum(hold_times) / len(hold_times) if hold_times else 0.0
            ),
            "rule_violations": rule_violation_count,
        }

    def _recommend(self, metrics: dict) -> list[dict]:
        recommendations: list[dict] = []
        if metrics["rule_violations"] > 0:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Review rule violations and document the setup conditions before increasing risk.",
                    "reason": "Journal entries contain rule violations.",
                }
            )
        if metrics["average_slippage_bps"] > 10:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Review execution timing because average slippage exceeded 10 bps.",
                    "reason": "Journal lifecycle metrics show elevated slippage.",
                }
            )
        if metrics["losing_trades"] > metrics["winning_trades"] and metrics["journal_entries"] > 0:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Review losing trade notes and tighten setup qualification criteria.",
                    "reason": "Journal entries show more losing trades than winning trades.",
                }
            )
        if not recommendations:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Continue paper trading with current controls; no automatic changes applied.",
                    "reason": "No critical review issues were detected.",
                }
            )
        return recommendations


class LearningJournalRepository:
    __slots__ = ("_session", "_store_strategy_recommendation", "_store_weekly_review")

    def __init__(self, repository: TradingRepository) -> None:
        self._session = repository.session
        self._store_weekly_review = repository.store_weekly_review
        self._store_strategy_recommendation = repository.store_strategy_recommendation

    def journal_entries_between(self, start: datetime, end: datetime) -> list[models.TradeJournal]:
        return list(
            self._session.scalars(
                select(models.TradeJournal).where(
                    models.TradeJournal.created_at >= start,
                    models.TradeJournal.created_at <= end,
                )
            ).all()
        )

    def store_weekly_review(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        summary: str,
        metrics: dict[str, Any],
        reason: str,
    ) -> models.WeeklyReview:
        return self._store_weekly_review(
            week_start=week_start,
            week_end=week_end,
            summary=summary,
            metrics=metrics,
            reason=reason,
        )

    def store_strategy_recommendation(
        self,
        *,
        strategy_id: str | None,
        recommendation: str,
        reason: str,
    ) -> models.StrategyRecommendation:
        return self._store_strategy_recommendation(
            strategy_id=strategy_id,
            recommendation=recommendation,
            reason=reason,
        )
