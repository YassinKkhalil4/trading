from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import EnvironmentMode, MarketRegime
from trading_system.app.execution.paper_execution import PaperExecutionEngine
from trading_system.app.execution.reconciliation import PositionSnapshot, reconcile_positions
from trading_system.app.features.calculations import LiquidityGates
from trading_system.app.risk.risk_engine import PortfolioState, RiskEngine
from trading_system.app.scanners.vwap_reclaim import VwapReclaimScanner, VwapReclaimSnapshot
from trading_system.app.signals.idempotency import DuplicateIdempotencyKeyError, IdempotencyRegistry
from trading_system.app.signals.signal_engine import SignalEngine
from trading_system.app.strategies.registry import StrategyRegistryService


def _snapshot():
    return VwapReclaimSnapshot(
        symbol="AMD",
        timestamp=datetime(2026, 6, 3, 10, 15, tzinfo=ZoneInfo("America/New_York")),
        price=101,
        previous_price=99,
        vwap=100,
        previous_vwap=100,
        relative_volume=2.2,
        average_volume=2_000_000,
        dollar_volume=80_000_000,
        spread_bps=8,
        market_regime=MarketRegime.BULL_TREND,
        has_catalyst=True,
    )


def _signal():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())
    return SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=_snapshot().timestamp,
        price=101,
        stop_loss=100,
    )


def test_vwap_reclaim_scanner_accepts_valid_setup():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())
    assert decision.accepted is True


def test_signal_engine_uses_configured_vwap_reclaim_rr_targets():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())

    signal = SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=_snapshot().timestamp,
        price=101,
        stop_loss=100,
        target_1_rr=1.5,
        target_2_rr=2.25,
    )

    assert signal.target_1 == 102.5
    assert signal.target_2 == 103.25
    assert signal.risk_reward == 1.5


def test_signal_idempotency_rejects_duplicate():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())
    registry = IdempotencyRegistry()
    engine = SignalEngine(idempotency_registry=registry)
    engine.create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=_snapshot().timestamp,
        price=101,
        stop_loss=100,
    )
    with pytest.raises(DuplicateIdempotencyKeyError):
        engine.create_vwap_reclaim_signal(
            scanner_decision=decision,
            source_timestamp=_snapshot().timestamp,
            price=101,
            stop_loss=100,
        )


def test_risk_rejects_max_trades_per_day():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())
    signal = SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=_snapshot().timestamp,
        price=101,
        stop_loss=100,
    )
    settings = Settings(environment_mode=EnvironmentMode.PAPER, max_trades_per_day=1)
    risk = RiskEngine(settings).evaluate(
        signal,
        PortfolioState(
            account_equity=100_000,
            open_positions=0,
            daily_loss_pct=0,
            weekly_loss_pct=0,
            sector_exposure_pct=0,
            trades_today=1,
            trades_by_strategy_today={},
        ),
    )
    assert risk.approved is False
    assert "Max trades per day" in risk.reason


@pytest.mark.parametrize(
    ("portfolio_overrides", "expected_reason"),
    [
        ({"symbol_exposure_pct": 20.0}, "Max symbol exposure reached."),
        ({"strategy_exposure_pct": 40.0}, "Max strategy exposure reached."),
        ({"correlated_exposure_pct": 50.0}, "Max correlated exposure reached."),
        ({"overnight_exposure_pct": 50.0}, "Max overnight exposure reached."),
        ({"event_risk_active": True}, "Event risk block is active."),
        ({"spread_bps": 21.0}, "Spread exceeds configured limit."),
        ({"expected_slippage_bps": 26.0}, "Expected slippage exceeds configured limit."),
    ],
)
def test_risk_rejects_expanded_exposure_event_spread_and_slippage_controls(
    portfolio_overrides,
    expected_reason,
):
    signal = _signal()
    portfolio = {
        "account_equity": 100_000,
        "open_positions": 0,
        "daily_loss_pct": 0,
        "weekly_loss_pct": 0,
        "sector_exposure_pct": 0,
        "trades_today": 0,
        "trades_by_strategy_today": {},
    }
    portfolio.update(portfolio_overrides)

    risk = RiskEngine(Settings(environment_mode=EnvironmentMode.PAPER)).evaluate(
        signal,
        PortfolioState(**portfolio),
    )

    assert risk.approved is False
    assert risk.reason == expected_reason


def test_reconciliation_blocks_mismatch():
    result = reconcile_positions([PositionSnapshot(symbol="AMD", internal_quantity=10, broker_quantity=9)])
    assert result.ok is False
    assert "mismatch" in result.reason


def test_paper_execution_requires_paper_mode():
    scanner = VwapReclaimScanner(
        liquidity_gates=LiquidityGates(),
        strategy_registry=StrategyRegistryService(),
    )
    decision = scanner.scan(_snapshot())
    signal = SignalEngine().create_vwap_reclaim_signal(
        scanner_decision=decision,
        source_timestamp=_snapshot().timestamp,
        price=101,
        stop_loss=100,
    )
    risk = RiskEngine(Settings(environment_mode=EnvironmentMode.RESEARCH)).evaluate(
        signal,
        PortfolioState(
            account_equity=100_000,
            open_positions=0,
            daily_loss_pct=0,
            weekly_loss_pct=0,
            sector_exposure_pct=0,
            trades_today=0,
            trades_by_strategy_today={},
        ),
    )
    order = PaperExecutionEngine(settings=Settings(environment_mode=EnvironmentMode.RESEARCH)).submit_limit_order(
        signal=signal,
        risk_decision=risk,
        reconciliation=reconcile_positions(
            [PositionSnapshot(symbol="AMD", internal_quantity=0, broker_quantity=0)]
        ),
    )
    assert order.quantity == 0
    assert "ENVIRONMENT_MODE=paper" in order.reason


def test_volatility_targeted_position_sizing_halves_allocation_when_volatility_doubles():
    from trading_system.app.risk.risk_engine import calculate_volatility_targeted_position_size_dollars

    low_vol_allocation = calculate_volatility_targeted_position_size_dollars(
        portfolio_value=100_000,
        current_annualized_volatility=0.40,
    )
    high_vol_allocation = calculate_volatility_targeted_position_size_dollars(
        portfolio_value=100_000,
        current_annualized_volatility=0.80,
    )

    assert high_vol_allocation == pytest.approx(low_vol_allocation / 2)


def test_risk_engine_blocks_non_positive_half_kelly_allocation():
    signal = _signal()
    risk = RiskEngine(Settings(environment_mode=EnvironmentMode.PAPER)).evaluate(
        signal,
        PortfolioState(
            account_equity=100_000,
            open_positions=0,
            daily_loss_pct=0,
            weekly_loss_pct=0,
            sector_exposure_pct=0,
            trades_today=0,
            trades_by_strategy_today={},
            annualized_volatility=0.4,
            half_kelly_weight=0.0,
        ),
    )

    assert risk.approved is False
    assert "Half-Kelly" in risk.reason


def test_ewma_true_range_uses_gap_aware_true_range():
    from trading_system.app.risk.risk_engine import calculate_ewma_true_range

    ewma = calculate_ewma_true_range(
        [
            {"high": 10.0, "low": 9.0, "close": 9.5},
            {"high": 12.0, "low": 11.0, "close": 11.5},
        ],
        periods=14,
    )

    assert ewma > 1.0
