from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.enums import StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.strategies.approval import StrategyApprovalWorkflow

STRATEGY_ID = "VWAP_RECLAIM"
STRATEGY_VERSION = "v1"


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _strategy(repo: TradingRepository) -> models.StrategyRegistry:
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(
            models.StrategyRegistry.strategy_id == STRATEGY_ID,
            models.StrategyRegistry.version == STRATEGY_VERSION,
        )
    )
    assert strategy is not None
    return strategy


def _set_research_evidence(
    repo: TradingRepository,
    *,
    backtest_trade_count: int = 30,
    out_of_sample_tested: bool = True,
    evidence_quality_score: float = 0.8,
) -> models.StrategyRegistry:
    strategy = _strategy(repo)
    strategy.backtest_trade_count = backtest_trade_count
    strategy.out_of_sample_tested = out_of_sample_tested
    strategy.evidence_quality_score = evidence_quality_score
    repo.session.commit()
    return strategy


def _approval_evidence() -> dict:
    return {
        "paper_positive_expectancy": True,
        "rule_violations": 0,
        "reconciliation_clean": True,
    }


def _promote_to_paper(repo: TradingRepository, workflow: StrategyApprovalWorkflow) -> None:
    report = repo.store_backtest_report(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        universe_name="evidence-test",
        assumptions={"slippage_bps": 5},
        metrics={"trade_count": 30, "profit_factor": 1.4},
        report_uri="s3://unit-test/backtests/vwap-reclaim.json",
        survivorship_bias_warning="Unit test report only.",
        reason="backtest evidence",
    )
    request = workflow.request_status_change(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        requested_status=StrategyStatus.PAPER_TESTING.value,
        requested_by="researcher",
        evidence={
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "backtest_report_id": report.id,
        },
        reason="backtest supports paper",
    )
    assert request.accepted is True
    decision = workflow.approve_status_change(
        request_id=request.request_id,
        approved=True,
        decided_by="admin",
        decision_reason="backtest reviewed",
    )
    assert decision.approved is True


def test_low_trade_count_blocks_approval():
    repo = _repo()
    workflow = StrategyApprovalWorkflow(repo)
    _promote_to_paper(repo, workflow)
    _set_research_evidence(
        repo,
        backtest_trade_count=10,
        out_of_sample_tested=True,
        evidence_quality_score=0.8,
    )

    request = workflow.request_status_change(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        requested_status=StrategyStatus.APPROVED_SMALL_SIZE.value,
        requested_by="admin",
        evidence=_approval_evidence(),
        reason="promote",
    )

    assert request.accepted is False
    assert "backtest_trade_count=10" in request.reason


def test_missing_out_of_sample_blocks_approval():
    repo = _repo()
    workflow = StrategyApprovalWorkflow(repo)
    _promote_to_paper(repo, workflow)
    _set_research_evidence(
        repo,
        backtest_trade_count=30,
        out_of_sample_tested=False,
        evidence_quality_score=0.8,
    )

    request = workflow.request_status_change(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        requested_status=StrategyStatus.APPROVED_SMALL_SIZE.value,
        requested_by="admin",
        evidence=_approval_evidence(),
        reason="promote",
    )

    assert request.accepted is False
    assert "out_of_sample_tested" in request.reason


def test_weak_evidence_score_blocks_approval():
    repo = _repo()
    workflow = StrategyApprovalWorkflow(repo)
    _promote_to_paper(repo, workflow)
    _set_research_evidence(
        repo,
        backtest_trade_count=30,
        out_of_sample_tested=True,
        evidence_quality_score=0.4,
    )

    request = workflow.request_status_change(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        requested_status=StrategyStatus.APPROVED_SMALL_SIZE.value,
        requested_by="admin",
        evidence=_approval_evidence(),
        reason="promote",
    )

    assert request.accepted is False
    assert "evidence_quality_score=0.4" in request.reason


def test_research_and_paper_transitions_still_allowed():
    repo = _repo()
    workflow = StrategyApprovalWorkflow(repo)
    strategy = _strategy(repo)
    assert strategy.status == StrategyStatus.RESEARCH.value

    report = repo.store_backtest_report(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        universe_name="evidence-test",
        assumptions={"slippage_bps": 5},
        metrics={"trade_count": 12, "profit_factor": 1.3},
        report_uri="s3://unit-test/backtests/vwap-reclaim.json",
        survivorship_bias_warning="Unit test report only.",
        reason="backtest evidence",
    )
    request = workflow.request_status_change(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        requested_status=StrategyStatus.PAPER_TESTING.value,
        requested_by="researcher",
        evidence={
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "backtest_report_id": report.id,
        },
        reason="backtest supports paper",
    )
    assert request.accepted is True

    decision = workflow.approve_status_change(
        request_id=request.request_id,
        approved=True,
        decided_by="admin",
        decision_reason="backtest reviewed",
    )
    assert decision.approved is True
    assert _strategy(repo).status == StrategyStatus.PAPER_TESTING.value
