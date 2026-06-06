from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_system.app.db.repositories import TradingRepository, model_to_dict


KILL_SWITCH_RULE_VERSION = "kill_switch_rules_v1"


@dataclass(frozen=True)
class KillSwitchActionResult:
    success: bool
    reason: str
    event: dict[str, Any] | None
    version: str = KILL_SWITCH_RULE_VERSION


class KillSwitchService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def activate(
        self,
        *,
        event_type: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> KillSwitchActionResult:
        if not reason.strip():
            return KillSwitchActionResult(False, "A reason is required to activate a kill switch.", None)
        row = self.repository.activate_kill_switch(
            event_type=event_type,
            reason=reason,
            payload=payload,
            actor=actor,
        )
        return KillSwitchActionResult(True, reason, model_to_dict(row))

    def resolve(self, *, event_id: str, reason: str, actor: str = "system") -> KillSwitchActionResult:
        if not reason.strip():
            return KillSwitchActionResult(False, "A resolution reason is required.", None)
        row = self.repository.resolve_kill_switch(
            event_id=event_id,
            resolution_reason=reason,
            actor=actor,
        )
        return KillSwitchActionResult(True, reason, model_to_dict(row))
