from __future__ import annotations

from trading_system.app.features.calculations import LiquidityGates, calculate_gap_pct, check_liquidity


def test_gap_pct():
    assert calculate_gap_pct(105, 100) == 5


def test_liquidity_gate_rejects_wide_spread():
    decision = check_liquidity(
        price=100,
        average_volume=2_000_000,
        dollar_volume=50_000_000,
        spread_bps=50,
        gates=LiquidityGates(max_spread_bps=20),
    )
    assert decision.passed is False
    assert "Spread" in decision.reason

