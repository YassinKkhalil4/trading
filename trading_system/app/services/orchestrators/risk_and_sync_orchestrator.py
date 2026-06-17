"""Broker synchronization, reconciliation, and live account control orchestration."""

from trading_system.app.services.runtime import TradingRuntimeService


class RiskAndSyncOrchestrator(TradingRuntimeService):
    """Orchestrates broker sync, reconciliation, cancellations, and flattening."""
