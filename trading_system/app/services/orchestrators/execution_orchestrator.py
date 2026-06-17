"""Order submission orchestration for paper, live, and internal broker orders."""

from trading_system.app.services.runtime import TradingRuntimeService


class ExecutionOrchestrator(TradingRuntimeService):
    """Orchestrates trade submission workflows only."""
