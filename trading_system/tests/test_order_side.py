from __future__ import annotations

import pytest

from trading_system.app.core.enums import Direction
from trading_system.app.execution.order_side import (
    entry_side_from_direction,
    exit_side_from_direction,
    normalize_order_side,
)


def test_long_entry_and_exit_mapping():
    assert entry_side_from_direction(Direction.LONG) == "buy"
    assert exit_side_from_direction(Direction.LONG) == "sell"


def test_short_entry_and_exit_mapping():
    assert entry_side_from_direction(Direction.SHORT) == "sell"
    assert exit_side_from_direction(Direction.SHORT) == "buy"


def test_normalize_order_side_maps_direction_labels():
    assert normalize_order_side("long") == "buy"
    assert normalize_order_side("short") == "sell"
    assert normalize_order_side("buy") == "buy"
    assert normalize_order_side("SELL") == "sell"


def test_normalize_order_side_rejects_unknown_without_direction():
    with pytest.raises(ValueError):
        normalize_order_side("unknown")
