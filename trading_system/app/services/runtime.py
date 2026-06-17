from __future__ import annotations

from trading_system.app.services.orchestrators.data_pipeline_orchestrator import DataPipelineOrchestrator
from trading_system.app.services.orchestrators.execution_orchestrator import ExecutionOrchestrator
from trading_system.app.services.orchestrators.research_orchestrator import ResearchOrchestrator
from trading_system.app.services.orchestrators.risk_and_sync_orchestrator import RiskAndSyncOrchestrator
from trading_system.app.services.runtime_support import PortfolioService, ScanCycleResult


class TradingRuntimeService(
    DataPipelineOrchestrator,
    ExecutionOrchestrator,
    RiskAndSyncOrchestrator,
    ResearchOrchestrator,
):
    """Backward-compatible facade for legacy callers.

    New code should import one of the focused orchestrators directly. This class
    intentionally contains no runtime logic; domain methods live in the
    orchestrator modules.
    """


__all__ = ["TradingRuntimeService", "PortfolioService", "ScanCycleResult"]

# Legacy tests and scripts monkeypatch adapter/collector symbols on this module.
# Propagate those assignments to the extracted orchestrator modules where the
# moved methods now resolve their globals.
import sys
from types import ModuleType

from trading_system.app.services import runtime_support as _runtime_support
from trading_system.app.services.orchestrators import data_pipeline_orchestrator as _data_orchestrator
from trading_system.app.services.orchestrators import execution_orchestrator as _execution_orchestrator
from trading_system.app.services.orchestrators import risk_and_sync_orchestrator as _sync_orchestrator

AlpacaBarsCollector = _runtime_support.AlpacaBarsCollector
AlpacaLiveAdapter = _runtime_support.AlpacaLiveAdapter
AlpacaPaperAdapter = _runtime_support.AlpacaPaperAdapter
YahooChartCollector = _runtime_support.YahooChartCollector

_PATCH_TARGETS = (
    _runtime_support,
    _data_orchestrator,
    _execution_orchestrator,
    _sync_orchestrator,
)


class _RuntimeCompatibilityModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("_"):
            return
        for module in _PATCH_TARGETS:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _RuntimeCompatibilityModule
