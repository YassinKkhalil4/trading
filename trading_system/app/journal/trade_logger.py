from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.enums import DecisionOutcome, DecisionType


JOURNAL_RULE_VERSION = "journal_v1"


@dataclass(frozen=True)
class JournalEntry:
    symbol: str
    strategy_id: str
    entry_thesis: str
    actual_entry: float | None = None
    actual_exit: float | None = None
    pnl: float | None = None
    mistake_tags: list[str] = field(default_factory=list)
    human_notes: str | None = None
    source_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    change_reason: str = "Manual journal entry created."


class TradeLogger:
    def __init__(self, decision_logger: InMemoryDecisionLogger | None = None) -> None:
        self.decision_logger = decision_logger or InMemoryDecisionLogger()
        self.entries: list[JournalEntry] = []

    def log_manual_trade(self, entry: JournalEntry) -> JournalEntry:
        self.entries.append(entry)
        self.decision_logger.record_simple(
            DecisionType.JOURNAL,
            DecisionOutcome.RECORDED,
            entry.change_reason,
            entity_id=entry.symbol,
            strategy_id=entry.strategy_id,
            rule_version=JOURNAL_RULE_VERSION,
        )
        return entry

