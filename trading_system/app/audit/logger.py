from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trading_system.app.core.enums import DecisionOutcome, DecisionType


@dataclass(frozen=True)
class DecisionLogEntry:
    decision_type: DecisionType
    outcome: DecisionOutcome
    reason: str
    entity_id: str | None = None
    strategy_id: str | None = None
    rule_version: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InMemoryDecisionLogger:
    """Small testable logger. Database persistence is represented by db.models.DecisionLog."""

    def __init__(self) -> None:
        self.entries: list[DecisionLogEntry] = []

    def record(self, entry: DecisionLogEntry) -> DecisionLogEntry:
        self.entries.append(entry)
        return entry

    def record_simple(
        self,
        decision_type: DecisionType,
        outcome: DecisionOutcome,
        reason: str,
        entity_id: str | None = None,
        strategy_id: str | None = None,
        rule_version: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DecisionLogEntry:
        return self.record(
            DecisionLogEntry(
                decision_type=decision_type,
                outcome=outcome,
                reason=reason,
                entity_id=entity_id,
                strategy_id=strategy_id,
                rule_version=rule_version,
                payload=payload or {},
            )
        )

