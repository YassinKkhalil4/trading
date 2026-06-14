from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from trading_system.app.core.config import Settings


class AbstractBrokerAdapter(ABC):
    """Broker execution interface used by live execution services."""

    settings: Settings

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Return whether the broker adapter has enough credentials to submit orders."""

    @abstractmethod
    async def submit_limit_bracket_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
        client_order_id: str,
    ) -> Any:
        """Submit a limit entry with attached take-profit and stop-loss orders."""

    @abstractmethod
    async def cancel_all_orders(self) -> Any:
        """Cancel all open broker orders."""
