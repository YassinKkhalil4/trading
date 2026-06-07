from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import desc, select

from trading_system.app.core.enums import StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.strategies.research_evidence import research_evidence_allows_approval


STRATEGY_APPROVAL_WORKFLOW_VERSION = "strategy_approval_workflow_v1"

STATUS_ORDER = [
    StrategyStatus.RESEARCH.value,
    StrategyStatus.PAPER_TESTING.value,
    StrategyStatus.APPROVED_SMALL_SIZE.value,
    StrategyStatus.APPROVED_FULL_SIZE.value,
]


@dataclass(frozen=True)
class StrategyApprovalDecision:
    request_id: str
    approved: bool
    reason: str
    version: str = STRATEGY_APPROVAL_WORKFLOW_VERSION


@dataclass(frozen=True)
class StrategyStatusRequestResult:
    success: bool
    request_id: str | None
    reason: str
    request: dict | None = None
    version: str = STRATEGY_APPROVAL_WORKFLOW_VERSION

    @property
    def accepted(self) -> bool:
        return self.success


@dataclass(frozen=True)
class StrategyStatusDecisionResult:
    success: bool
    request_id: str
    approved: bool
    reason: str
    request: dict | None = None
    version: str = STRATEGY_APPROVAL_WORKFLOW_VERSION


class StrategyApprovalWorkflow:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def request_status_change(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        requested_status: str,
        requested_by: str,
        evidence: dict,
        reason: str,
    ) -> StrategyStatusRequestResult:
        strategy = self._strategy(strategy_id, strategy_version)
        if not strategy:
            return StrategyStatusRequestResult(False, None, f"Unknown strategy/version: {strategy_id}/{strategy_version}")
        missing = self._missing_request_evidence(strategy.status, requested_status, evidence)
        if missing:
            return StrategyStatusRequestResult(
                False,
                None,
                "Strategy promotion is blocked by missing evidence: " + ", ".join(missing),
            )
        if requested_status in {
            StrategyStatus.APPROVED_SMALL_SIZE.value,
            StrategyStatus.APPROVED_FULL_SIZE.value,
        }:
            allowed, research_reason = research_evidence_allows_approval(strategy)
            if not allowed:
                return StrategyStatusRequestResult(False, None, research_reason)
        row = self.repository.request_strategy_status_change(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            requested_status=requested_status,
            current_status=strategy.status,
            requested_by=requested_by,
            evidence=evidence,
            reason=reason,
        )
        return StrategyStatusRequestResult(True, row.id, reason, _model_payload(row))

    def request_status_change_result(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        requested_status: str,
        requested_by: str,
        evidence: dict,
        reason: str,
    ) -> StrategyStatusRequestResult:
        try:
            result = self.request_status_change(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                requested_status=requested_status,
                requested_by=requested_by,
                evidence=evidence,
                reason=reason,
            )
        except ValueError as exc:
            return StrategyStatusRequestResult(False, None, str(exc))
        return result

    def approve(self, *, request_id: str, approved_by: str, reason: str) -> StrategyApprovalDecision:
        request = self.repository.session.get(models.StrategyApprovalRequest, request_id)
        if not request:
            raise ValueError(f"Unknown strategy approval request: {request_id}")
        allowed, validation_reason = self._validate_request(request)
        if not allowed:
            self.repository.decide_strategy_status_change(
                request_id=request_id,
                approved=False,
                decided_by=approved_by,
                decision_reason=validation_reason,
            )
            return StrategyApprovalDecision(request_id, False, validation_reason)
        row = self.repository.decide_strategy_status_change(
            request_id=request_id,
            approved=True,
            decided_by=approved_by,
            decision_reason=reason,
        )
        return StrategyApprovalDecision(row.id, True, reason)

    def reject(self, *, request_id: str, rejected_by: str, reason: str) -> StrategyApprovalDecision:
        row = self.repository.decide_strategy_status_change(
            request_id=request_id,
            approved=False,
            decided_by=rejected_by,
            decision_reason=reason,
        )
        return StrategyApprovalDecision(row.id, False, reason)

    def approve_status_change(
        self,
        *,
        request_id: str,
        approved: bool,
        decided_by: str,
        decision_reason: str,
    ) -> StrategyStatusDecisionResult:
        decision = (
            self.approve(request_id=request_id, approved_by=decided_by, reason=decision_reason)
            if approved
            else self.reject(request_id=request_id, rejected_by=decided_by, reason=decision_reason)
        )
        request = self.repository.session.get(models.StrategyApprovalRequest, request_id)
        return StrategyStatusDecisionResult(
            success=True,
            request_id=decision.request_id,
            approved=decision.approved,
            reason=decision.reason,
            request=_model_payload(request),
        )

    def _validate_request(self, request: models.StrategyApprovalRequest) -> tuple[bool, str]:
        if request.requested_status in {StrategyStatus.PAUSED.value, StrategyStatus.RETIRED.value}:
            return True, "Pause/retire transition accepted with human reason."
        if request.current_status not in STATUS_ORDER or request.requested_status not in STATUS_ORDER:
            return False, "Unknown strategy status transition."
        current_idx = STATUS_ORDER.index(request.current_status)
        requested_idx = STATUS_ORDER.index(request.requested_status)
        if requested_idx != current_idx + 1:
            return False, "Strategy promotion must move exactly one status forward."
        if request.requested_status == StrategyStatus.PAPER_TESTING.value:
            report = self._latest_backtest_report(request.strategy_id, request.strategy_version)
            if not report:
                return False, "Backtest report evidence is required before PAPER_TESTING."
            metrics = report.metrics or {}
            trade_count = int(metrics.get("trade_count") or metrics.get("Total Trades") or 0)
            profit_factor = float(metrics.get("profit_factor") or metrics.get("Profit Factor") or 0)
            if trade_count <= 0:
                return False, "Backtest evidence has no recorded trades."
            if profit_factor <= 0:
                return False, "Backtest evidence must include positive profit factor."
        if request.requested_status in {
            StrategyStatus.APPROVED_SMALL_SIZE.value,
            StrategyStatus.APPROVED_FULL_SIZE.value,
        }:
            evidence = request.evidence or {}
            if not evidence.get("paper_positive_expectancy"):
                return False, "Paper positive expectancy evidence is required."
            if evidence.get("rule_violations", 1) != 0:
                return False, "Strategy cannot be promoted with recorded rule violations."
            if evidence.get("reconciliation_clean") is not True:
                return False, "Clean broker/internal reconciliation evidence is required."
            strategy = self._strategy(request.strategy_id, request.strategy_version)
            allowed, research_reason = research_evidence_allows_approval(strategy)
            if not allowed:
                return False, research_reason
        return True, "Strategy approval evidence passed."

    def _strategy(self, strategy_id: str, strategy_version: str) -> models.StrategyRegistry | None:
        return self.repository.session.scalar(
            select(models.StrategyRegistry).where(
                models.StrategyRegistry.strategy_id == strategy_id,
                models.StrategyRegistry.version == strategy_version,
            )
        )

    def _missing_request_evidence(
        self,
        current_status: str,
        requested_status: str,
        evidence: dict,
    ) -> list[str]:
        if requested_status in {StrategyStatus.PAUSED.value, StrategyStatus.RETIRED.value}:
            return []
        if current_status not in STATUS_ORDER or requested_status not in STATUS_ORDER:
            return ["valid_status_transition"]
        if STATUS_ORDER.index(requested_status) != STATUS_ORDER.index(current_status) + 1:
            return ["one_step_promotion"]
        if requested_status == StrategyStatus.PAPER_TESTING.value:
            report = self._backtest_report_for_evidence(
                strategy_id=evidence.get("strategy_id") or "",
                strategy_version=evidence.get("strategy_version") or "",
                evidence=evidence,
            )
            if not report:
                return ["persisted_backtest_report"]
        if requested_status in {
            StrategyStatus.APPROVED_SMALL_SIZE.value,
            StrategyStatus.APPROVED_FULL_SIZE.value,
        }:
            missing = []
            if evidence.get("paper_positive_expectancy") is not True:
                missing.append("paper_positive_expectancy")
            if evidence.get("rule_violations") != 0:
                missing.append("rule_violations=0")
            if evidence.get("reconciliation_clean") is not True:
                missing.append("reconciliation_clean")
            return missing
        return []

    def _latest_backtest_report(
        self,
        strategy_id: str,
        strategy_version: str,
    ) -> models.BacktestReport | None:
        return self.repository.session.scalar(
            select(models.BacktestReport)
            .where(
                models.BacktestReport.strategy_id == strategy_id,
                models.BacktestReport.strategy_version == strategy_version,
            )
            .order_by(desc(models.BacktestReport.created_at))
            .limit(1)
        )

    def _backtest_report_for_evidence(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        evidence: dict,
    ) -> models.BacktestReport | None:
        report_id = evidence.get("backtest_report_id")
        if report_id:
            report = self.repository.session.get(models.BacktestReport, report_id)
            if (
                report
                and report.strategy_id == strategy_id
                and report.strategy_version == strategy_version
            ):
                return report
            return None
        return self._latest_backtest_report(strategy_id, strategy_version)


def _model_payload(row) -> dict | None:
    if row is None:
        return None
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
