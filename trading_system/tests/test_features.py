from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from trading_system.app.features.calculations import (
    InvalidFeatureData,
    LiquidityGates,
    calculate_gap_pct,
    check_liquidity,
    compute_core_features,
)


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


def test_core_features_block_invalid_candle_status():
    frame = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 6, 3, 13, 30, tzinfo=UTC),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
                "data_quality_status": "VALID",
            },
            {
                "timestamp": datetime(2026, 6, 3, 13, 31, tzinfo=UTC),
                "open": 140.0,
                "high": 142.0,
                "low": 139.0,
                "close": 141.0,
                "volume": 2000.0,
                "data_quality_status": "SUSPICIOUS_PRICE",
            },
        ]
    ).set_index("timestamp")

    with pytest.raises(InvalidFeatureData):
        compute_core_features(frame, previous_close=99.0)


def test_core_features_calculate_required_metrics():
    frame = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 6, 3, 13, 30, tzinfo=UTC),
                "open": 105.0,
                "high": 106.0,
                "low": 104.0,
                "close": 105.0,
                "volume": 1000.0,
                "data_quality_status": "VALID",
            },
            {
                "timestamp": datetime(2026, 6, 3, 13, 31, tzinfo=UTC),
                "open": 105.0,
                "high": 108.0,
                "low": 104.0,
                "close": 107.0,
                "volume": 2000.0,
                "data_quality_status": "VALID",
            },
        ]
    ).set_index("timestamp")

    features = compute_core_features(frame, previous_close=100.0, average_volume=1000.0)

    assert features.vwap > 0
    assert features.atr == 3
    assert features.premarket_gap_pct == 5
    assert features.relative_volume == 2
