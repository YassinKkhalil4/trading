from __future__ import annotations

from trading_system.app.core.enums import Direction

BUY = "buy"
SELL = "sell"


def entry_side_from_direction(direction: Direction | str) -> str:
    if isinstance(direction, Direction):
        value = direction.value
    else:
        value = str(direction).upper()
    if value == Direction.LONG.value:
        return BUY
    if value == Direction.SHORT.value:
        return SELL
    lowered = str(direction).lower()
    if lowered in (BUY, SELL):
        return lowered
    raise ValueError(f"Unsupported trade direction: {direction}")


def exit_side_from_direction(direction: Direction | str) -> str:
    entry = entry_side_from_direction(direction)
    return SELL if entry == BUY else BUY


def normalize_order_side(side: str, *, direction: Direction | str | None = None) -> str:
    lowered = (side or "").strip().lower()
    if lowered in (BUY, SELL):
        return lowered
    if lowered == "long":
        return BUY
    if lowered == "short":
        return SELL
    if direction is not None:
        return entry_side_from_direction(direction)
    raise ValueError(f"Unsupported order side: {side}")
