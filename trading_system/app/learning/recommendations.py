from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

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
        self.repository = repository

    def run_weekly_review(self) -> LearningRunResult:
        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)
        metrics = self._metrics(week_start, now)
        summary = (
            f"Weekly review: {metrics['journal_entries']} journal entries, "
            f"{metrics['risk_rejections']} risk rejections, {metrics['orders']} orders, "
            f"{metrics['fills']} fills."
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
        return {
            "journal_entries": _count_between(self.repository, models.TradeJournal, start, end),
            "risk_rejections": int(
                self.repository.session.scalar(
                    select(func.count())
                    .select_from(models.RiskCheck)
                    .where(models.RiskCheck.created_at >= start, models.RiskCheck.approved.is_(False))
                )
                or 0
            ),
            "orders": _count_between(self.repository, models.Order, start, end),
            "fills": _count_between(self.repository, models.Fill, start, end),
            "rule_violations": int(
                self.repository.session.scalar(
                    select(func.count())
                    .select_from(models.TradeJournal)
                    .where(models.TradeJournal.created_at >= start, models.TradeJournal.rule_violations != [])
                )
                or 0
            ),
        }

    def _recommend(self, metrics: dict) -> list[dict]:
        recommendations: list[dict] = []
        if metrics["risk_rejections"] > 0:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Review rejected signals and tighten scanner thresholds before increasing size.",
                    "reason": "Risk rejections occurred during the review window.",
                }
            )
        if metrics["fills"] == 0 and metrics["orders"] > 0:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Inspect limit prices and stale-order cancellation rules.",
                    "reason": "Orders existed without reconciled fills.",
                }
            )
        if metrics["rule_violations"] > 0:
            recommendations.append(
                {
                    "strategy_id": None,
                    "recommendation": "Pause affected strategies until rule violations are reviewed.",
                    "reason": "Journal entries contain rule violations.",
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


def _count_between(repository: TradingRepository, model: type, start: datetime, end: datetime) -> int:
    return int(
        repository.session.scalar(
            select(func.count()).select_from(model).where(model.created_at >= start, model.created_at <= end)
        )
        or 0
    )
