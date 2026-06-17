from __future__ import annotations

import ast
from pathlib import Path

from trading_system.app.services.runtime import TradingRuntimeService
from trading_system.app.services.orchestrators.data_pipeline_orchestrator import DataPipelineOrchestrator
from trading_system.app.services.orchestrators.execution_orchestrator import ExecutionOrchestrator
from trading_system.app.services.orchestrators.research_orchestrator import ResearchOrchestrator
from trading_system.app.services.orchestrators.risk_and_sync_orchestrator import RiskAndSyncOrchestrator


ORCHESTRATOR_MODULES = [
    Path("trading_system/app/services/orchestrators/data_pipeline_orchestrator.py"),
    Path("trading_system/app/services/orchestrators/execution_orchestrator.py"),
    Path("trading_system/app/services/orchestrators/research_orchestrator.py"),
    Path("trading_system/app/services/orchestrators/risk_and_sync_orchestrator.py"),
]


def test_runtime_facade_and_orchestrators_import_without_circular_dependency() -> None:
    assert TradingRuntimeService
    assert DataPipelineOrchestrator
    assert ExecutionOrchestrator
    assert ResearchOrchestrator
    assert RiskAndSyncOrchestrator


def test_orchestrators_do_not_import_runtime_facade() -> None:
    for module_path in ORCHESTRATOR_MODULES:
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "trading_system.app.services.runtime", module_path
            elif isinstance(node, ast.Import):
                imported_names = {alias.name for alias in node.names}
                assert "trading_system.app.services.runtime" not in imported_names, module_path
