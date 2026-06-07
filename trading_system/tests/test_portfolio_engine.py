from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import Direction, SignalStatus, TradeType
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.services.portfolio.portfolio_engine import (
    PortfolioDecisionOutcome,
    PortfolioDecisionService,
    PortfolioEvaluationContext,
    PortfolioOpenOrder,
    PortfolioPosition,
)
from trading_system.app.signals.signal_engine import TradeSignal


SEMICONDUCTOR_SYMBOLS = ("NVDA", "AMD", "INTC", "MU", "AVGO")
SEMICONDUCTOR_SECTORS = {symbol: "Semiconductors" for symbol in SEMICONDUCTOR_SYMBOLS}


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _signal(
    *,
    symbol: str,
    strategy_id: str = "VWAP_RECLAIM",
    entry: float = 100.0,
    stop_loss: float = 99.0,
    trade_type: TradeType = TradeType.DAY_TRADE,
) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        strategy_id=strategy_id,
        strategy_version="v1",
        trade_type=trade_type,
        direction=Direction.LONG,
        entry_zone=(entry, entry * 1.001),
        stop_loss=stop_loss,
        target_1=entry + (entry - stop_loss) * 2,
        target_2=entry + (entry - stop_loss) * 3,
        risk_reward=2.0,
        confidence_score=90.0,
        time_horizon="intraday",
        invalidation="Loss of VWAP with rising sell volume.",
        source_timestamp=datetime(2026, 6, 3, 10, 15, tzinfo=UTC),
        idempotency_key=f"signal-{symbol.lower()}-test",
        status=SignalStatus.CANDIDATE,
    )


def _settings(**overrides) -> Settings:
    defaults = {
        "risk_per_trade_pct": 0.25,
        "max_open_positions": 3,
        "max_single_sector_exposure_pct": 30.0,
        "max_symbol_exposure_pct": 20.0,
        "max_strategy_exposure_pct": 40.0,
        "max_correlated_exposure_pct": 50.0,
        "max_overnight_exposure_pct": 50.0,
        "min_cash_buffer_pct": 10.0,
        "max_same_sector_new_signals": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _service(**settings_overrides) -> PortfolioDecisionService:
    return PortfolioDecisionService(settings=_settings(**settings_overrides))


def _evaluate_batch(
    service: PortfolioDecisionService,
    symbols: tuple[str, ...],
    *,
    context: PortfolioEvaluationContext | None = None,
) -> list:
    context = context or PortfolioEvaluationContext(
        account_equity=100_000,
        account_cash=100_000,
        symbol_sectors=SEMICONDUCTOR_SECTORS,
    )
    decisions = []
    approved_in_sector = 0
    for index, symbol in enumerate(symbols):
        decision = service.evaluate(
            signal_id=f"sig-{symbol}-{index}",
            signal=_signal(symbol=symbol),
            context=PortfolioEvaluationContext(
                account_equity=context.account_equity,
                account_cash=context.account_cash,
                positions=context.positions,
                open_orders=context.open_orders,
                symbol_sectors=context.symbol_sectors,
                correlated_groups=context.correlated_groups,
                pending_same_sector_signal_count=approved_in_sector,
                approved_same_sector_signal_count=approved_in_sector,
            ),
            persist=False,
        )
        decisions.append(decision)
        if decision.approved:
            approved_in_sector += 1
    return decisions


def test_five_semiconductor_signals_are_not_all_approved_together():
    service = _service(max_open_positions=3, max_same_sector_new_signals=2)
    decisions = _evaluate_batch(service, SEMICONDUCTOR_SYMBOLS)

    approved = [decision for decision in decisions if decision.approved]
    rejected = [decision for decision in decisions if not decision.approved]

    assert len(approved) < len(SEMICONDUCTOR_SYMBOLS)
    assert len(rejected) >= 2
    assert all(decision.outcome == PortfolioDecisionOutcome.REJECTED for decision in rejected[-2:])


def test_excessive_sector_exposure_rejects():
    service = _service(max_single_sector_exposure_pct=25.0)
    context = PortfolioEvaluationContext(
        account_equity=100_000,
        account_cash=100_000,
        positions=(
            PortfolioPosition(
                symbol="AMD",
                quantity=100,
                average_price=250.0,
                sector="Semiconductors",
                strategy_id="VWAP_RECLAIM",
            ),
        ),
        symbol_sectors=SEMICONDUCTOR_SECTORS,
    )
    decision = service.evaluate(
        signal_id="sig-nvda-sector",
        signal=_signal(symbol="NVDA"),
        context=context,
        persist=False,
    )

    assert decision.outcome == PortfolioDecisionOutcome.REJECTED
    assert decision.recommended_size_multiplier == 0.0
    assert any("sector" in reason.lower() for reason in decision.reasons)


def test_excessive_symbol_exposure_rejects():
    service = _service(max_symbol_exposure_pct=15.0)
    context = PortfolioEvaluationContext(
        account_equity=100_000,
        account_cash=100_000,
        positions=(
            PortfolioPosition(
                symbol="AMD",
                quantity=100,
                average_price=150.0,
                sector="Semiconductors",
                strategy_id="VWAP_RECLAIM",
            ),
        ),
        symbol_sectors=SEMICONDUCTOR_SECTORS,
    )
    decision = service.evaluate(
        signal_id="sig-amd-symbol",
        signal=_signal(symbol="AMD"),
        context=context,
        persist=False,
    )

    assert decision.outcome == PortfolioDecisionOutcome.REJECTED
    assert any("symbol" in reason.lower() for reason in decision.reasons)


def test_acceptable_diversified_signal_approves():
    service = _service(max_symbol_exposure_pct=30.0)
    context = PortfolioEvaluationContext(
        account_equity=100_000,
        account_cash=100_000,
        positions=(
            PortfolioPosition(
                symbol="JPM",
                quantity=50,
                average_price=180.0,
                sector="Financial Services",
                strategy_id="VWAP_RECLAIM",
            ),
        ),
        symbol_sectors={
            **SEMICONDUCTOR_SECTORS,
            "JPM": "Financial Services",
            "XLV": "Healthcare",
        },
    )
    decision = service.evaluate(
        signal_id="sig-xlv-diversified",
        signal=_signal(symbol="XLV"),
        context=context,
        persist=False,
    )

    assert decision.outcome == PortfolioDecisionOutcome.APPROVED
    assert decision.recommended_size_multiplier == 1.0
    assert decision.reasons == ("Portfolio checks approved.",)


def test_reduced_size_decision_is_possible():
    service = _service(max_single_sector_exposure_pct=27.0, min_cash_buffer_pct=5.0)
    context = PortfolioEvaluationContext(
        account_equity=100_000,
        account_cash=100_000,
        positions=(
            PortfolioPosition(
                symbol="AMD",
                quantity=100,
                average_price=170.0,
                sector="Semiconductors",
                strategy_id="VWAP_RECLAIM",
            ),
        ),
        symbol_sectors=SEMICONDUCTOR_SECTORS,
    )
    decision = service.evaluate(
        signal_id="sig-nvda-reduced",
        signal=_signal(symbol="NVDA"),
        context=context,
        persist=False,
    )

    assert decision.outcome == PortfolioDecisionOutcome.REDUCED_SIZE
    assert 0.0 < decision.recommended_size_multiplier < 1.0
    assert any("reduction" in reason.lower() for reason in decision.reasons)


def test_portfolio_decision_persists_to_decision_logs():
    repo = _repo()
    service = PortfolioDecisionService(settings=_settings(), repository=repo)
    decision = service.evaluate(
        signal_id="sig-persist",
        signal=_signal(symbol="AMD"),
        context=PortfolioEvaluationContext(
            account_equity=100_000,
            account_cash=100_000,
            symbol_sectors=SEMICONDUCTOR_SECTORS,
        ),
        persist=True,
    )

    row = repo.session.scalar(
        select(models.DecisionLog).where(models.DecisionLog.entity_id == "sig-persist")
    )
    assert row is not None
    assert row.entity_type == "portfolio_decision"
    assert row.payload["portfolio_outcome"] == decision.outcome.value
    assert row.payload["recommended_size_multiplier"] == decision.recommended_size_multiplier
