"""Research, backtesting, readiness, and dashboard orchestration."""

from trading_system.app.services.runtime import TradingRuntimeService


class ResearchOrchestrator(TradingRuntimeService):
    """Orchestrates research/reporting workflows only."""
