from __future__ import annotations

from dataclasses import dataclass

from trading_system.app.core.enums import StrategyStatus, TradeType
from trading_system.app.db.seed import DEFAULT_STRATEGIES


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    name: str
    version: str
    status: StrategyStatus
    trade_type: TradeType
    allowed_timeframes: list[str]
    allowed_regimes: list[str]
    requires_human_approval: bool = True
    logic_version: str = "v1"
    reason: str = ""

    @property
    def live_trade_allowed(self) -> bool:
        return self.status in {
            StrategyStatus.APPROVED_SMALL_SIZE,
            StrategyStatus.APPROVED_FULL_SIZE,
        }

    @property
    def paper_trade_allowed(self) -> bool:
        return self.status in {
            StrategyStatus.PAPER_TESTING,
            StrategyStatus.APPROVED_SMALL_SIZE,
            StrategyStatus.APPROVED_FULL_SIZE,
        }


class StrategyRegistryService:
    def __init__(self, strategies: list[StrategyDefinition] | None = None) -> None:
        self._strategies = {
            strategy.strategy_id: strategy for strategy in (strategies or default_strategy_definitions())
        }

    def get(self, strategy_id: str) -> StrategyDefinition:
        try:
            return self._strategies[strategy_id]
        except KeyError as exc:
            raise KeyError(f"Unknown strategy: {strategy_id}") from exc

    def can_generate_research_signal(self, strategy_id: str) -> tuple[bool, str]:
        strategy = self.get(strategy_id)
        if strategy.status in {StrategyStatus.PAUSED, StrategyStatus.RETIRED}:
            return False, f"Strategy is {strategy.status.value}: {strategy.reason}"
        return True, "Strategy is available for research signal generation."

    def can_paper_trade(self, strategy_id: str) -> tuple[bool, str]:
        strategy = self.get(strategy_id)
        if not strategy.paper_trade_allowed:
            return False, f"Strategy status {strategy.status.value} is not allowed for paper execution."
        return True, "Strategy is allowed for paper execution."

    def all(self) -> list[StrategyDefinition]:
        return list(self._strategies.values())


def default_strategy_definitions() -> list[StrategyDefinition]:
    strategies = []
    for seed in DEFAULT_STRATEGIES:
        strategies.append(
            StrategyDefinition(
                strategy_id=seed["strategy_id"],
                name=seed["name"],
                version=seed["version"],
                status=StrategyStatus(seed["status"]),
                trade_type=TradeType(seed["trade_type"]),
                allowed_timeframes=list(seed["allowed_timeframes"]),
                allowed_regimes=list(seed["allowed_regimes"]),
                requires_human_approval=bool(seed.get("requires_human_approval", True)),
                logic_version=seed.get("logic_version", "v1"),
                reason=seed.get("changed_reason", ""),
            )
        )
    return strategies

