from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from trading_system.app.ai.decision_support import build_artifact_payload, get_decision_support_provider
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
        provider = get_decision_support_provider()
        recommendation_outputs = provider.recommend_weekly_actions(
            {"week_start": week_start, "week_end": now, "metrics": metrics}
        )
        artifact = build_artifact_payload(
            artifact_type="WEEKLY_RECOMMENDATIONS",
            provider=provider,
            prompt_version=LEARNING_ENGINE_VERSION,
            input_payload={"week_start": week_start, "week_end": now, "metrics": metrics},
            output={"recommendations": [item.__dict__ for item in recommendation_outputs]},
            fallback_used=True,
        )
        artifact_row = self.repository.store_decision_support_artifact(
            artifact,
            reason="Decision-support weekly recommendations generated from journal metrics.",
            source_timestamp=now,
        )
        review = self.repository.store_weekly_review(
            week_start=week_start,
            week_end=now,
            summary=summary,
            metrics=metrics,
            reason="Automated weekly review generated recommendations only.",
        )
        accepted_payloads = (
            artifact.output_payload.get("recommendations", []) if artifact.validation.accepted else []
        )
        for item in accepted_payloads:
            self.repository.store_strategy_recommendation(
                strategy_id=item["strategy_id"],
                recommendation=item["recommendation"],
                severity=item.get("severity", "LOW"),
                reason=item["reason"],
                evidence=item.get("supporting_metrics") or metrics,
                decision_support_artifact_id=artifact_row.id,
            )
        return LearningRunResult(
            weekly_review_id=review.id,
            recommendations_created=len(accepted_payloads),
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
        by_strategy: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = entry.strategy_id or "UNKNOWN"
            bucket = by_strategy.setdefault(key, {"trades": 0, "pnl": 0.0, "rule_violations": 0})
            bucket["trades"] += 1
            bucket["pnl"] += float(entry.pnl or 0.0)
            bucket["rule_violations"] += 1 if entry.rule_violations else 0
        by_regime: dict[str, int] = {}
        by_catalyst: dict[str, int] = {}
        for entry in entries:
            if entry.market_regime:
                by_regime[entry.market_regime] = by_regime.get(entry.market_regime, 0) + 1
            if entry.catalyst:
                by_catalyst[entry.catalyst] = by_catalyst.get(entry.catalyst, 0) + 1
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
            "by_strategy": by_strategy,
            "by_regime": by_regime,
            "by_catalyst": by_catalyst,
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
    __slots__ = (
        "_session",
        "_store_decision_support_artifact",
        "_store_strategy_recommendation",
        "_store_weekly_review",
    )

    def __init__(self, repository: TradingRepository) -> None:
        self._session = repository.session
        self._store_weekly_review = repository.store_weekly_review
        self._store_strategy_recommendation = repository.store_strategy_recommendation
        self._store_decision_support_artifact = repository.store_decision_support_artifact

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
        severity: str = "LOW",
        evidence: dict[str, Any] | None = None,
        decision_support_artifact_id: str | None = None,
    ) -> models.StrategyRecommendation:
        return self._store_strategy_recommendation(
            strategy_id=strategy_id,
            recommendation=recommendation,
            severity=severity,
            reason=reason,
            evidence=evidence,
            decision_support_artifact_id=decision_support_artifact_id,
        )

    def store_decision_support_artifact(
        self,
        artifact,
        *,
        reason: str,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionSupportArtifact:
        return self._store_decision_support_artifact(
            artifact,
            reason=reason,
            source_timestamp=source_timestamp,
        )
