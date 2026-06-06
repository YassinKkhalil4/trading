from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    internal_quantity: float
    broker_quantity: float


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    reason: str


def reconcile_positions(positions: list[PositionSnapshot]) -> ReconciliationResult:
    mismatches = [
        item
        for item in positions
        if round(float(item.internal_quantity), 6) != round(float(item.broker_quantity), 6)
    ]
    if mismatches:
        symbols = ", ".join(item.symbol for item in mismatches)
        return ReconciliationResult(False, f"Broker/internal position mismatch for: {symbols}.")
    return ReconciliationResult(True, "Broker/internal positions reconciled.")

