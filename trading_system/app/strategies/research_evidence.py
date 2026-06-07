from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.db import models


DEFAULT_MINIMUM_TRADE_COUNT_REQUIRED = 30
MIN_EVIDENCE_QUALITY_SCORE = 0.6


@dataclass(frozen=True)
class StrategyResearchEvidenceMetadata:
    minimum_trade_count_required: int
    backtest_trade_count: int
    out_of_sample_tested: bool
    walk_forward_tested: bool
    parameter_sensitivity_score: float | None
    paper_forward_test_days: int | None
    evidence_quality_score: float | None

    @classmethod
    def from_strategy(cls, strategy: models.StrategyRegistry) -> StrategyResearchEvidenceMetadata:
        return cls(
            minimum_trade_count_required=int(
                strategy.minimum_trade_count_required or DEFAULT_MINIMUM_TRADE_COUNT_REQUIRED
            ),
            backtest_trade_count=int(strategy.backtest_trade_count or 0),
            out_of_sample_tested=bool(strategy.out_of_sample_tested),
            walk_forward_tested=bool(strategy.walk_forward_tested),
            parameter_sensitivity_score=strategy.parameter_sensitivity_score,
            paper_forward_test_days=strategy.paper_forward_test_days,
            evidence_quality_score=strategy.evidence_quality_score,
        )

    def approval_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.backtest_trade_count < self.minimum_trade_count_required:
            blockers.append(
                f"backtest_trade_count={self.backtest_trade_count} "
                f"(minimum {self.minimum_trade_count_required})"
            )
        if not self.out_of_sample_tested:
            blockers.append("out_of_sample_tested")
        if (
            self.evidence_quality_score is None
            or self.evidence_quality_score < MIN_EVIDENCE_QUALITY_SCORE
        ):
            blockers.append(
                f"evidence_quality_score={self.evidence_quality_score} "
                f"(minimum {MIN_EVIDENCE_QUALITY_SCORE})"
            )
        return blockers


def research_evidence_allows_approval(
    strategy: models.StrategyRegistry | None,
) -> tuple[bool, str]:
    if strategy is None:
        return False, "Strategy not found for research evidence validation."
    blockers = StrategyResearchEvidenceMetadata.from_strategy(strategy).approval_blockers()
    if blockers:
        return False, "Research evidence insufficient: " + ", ".join(blockers)
    return True, "Research evidence validation passed."
