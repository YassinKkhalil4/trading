from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading_system.app.core.enums import OrderStatus


EXECUTION_RULE_VERSION = "paper_execution_v1"


@dataclass(frozen=True)
class PaperOrder:
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float
    stop_loss: float
    idempotency_key: str
    status: OrderStatus
    reason: str
    created_at: datetime


class LiveExecutionEngine:
    def submit_order(self, *_args, **_kwargs):
        raise RuntimeError(
            "Use LiveExecutionService with explicit live gates; live trading is disabled by default."
        )
