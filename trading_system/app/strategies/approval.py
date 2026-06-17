from __future__ import annotations


class StrategyApprovalWorkflow:
    """Removed: strategy status changes are now code/config controlled, not human approval rows."""

    def __init__(self, *_args, **_kwargs) -> None:
        raise RuntimeError("StrategyApprovalWorkflow has been removed; use code-reviewed strategy registry changes.")
