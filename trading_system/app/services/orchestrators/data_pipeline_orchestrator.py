"""Data ingestion, feature generation, catalyst, universe, and scanner orchestration.

This class is the Celery-facing boundary for market-data pipeline jobs.  It
currently delegates shared runtime helpers to ``TradingRuntimeService`` while
providing a narrow import target for workers so ingestion tasks do not depend on
execution or broker-control orchestrators.
"""

from trading_system.app.services.runtime import TradingRuntimeService


class DataPipelineOrchestrator(TradingRuntimeService):
    """Orchestrates data collection and scanner pipeline work only."""
