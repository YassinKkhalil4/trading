from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Cooldown:
    symbol: str
    strategy_id: str
    cooldown_until: datetime
    reason: str


class StrategyCooldownBook:
    def __init__(self) -> None:
        self._cooldowns: dict[tuple[str, str], Cooldown] = {}

    def add_stopout_cooldown(
        self,
        *,
        symbol: str,
        strategy_id: str,
        now: datetime | None = None,
        minutes: int = 60,
        reason: str = "Stop-out cooldown.",
    ) -> Cooldown:
        now = now or datetime.now(UTC)
        cooldown = Cooldown(
            symbol=symbol,
            strategy_id=strategy_id,
            cooldown_until=now + timedelta(minutes=minutes),
            reason=reason,
        )
        self._cooldowns[(symbol, strategy_id)] = cooldown
        return cooldown

    def add_failed_signal_cooldown(
        self,
        *,
        symbol: str,
        strategy_id: str,
        now: datetime | None = None,
        minutes: int = 30,
        reason: str = "Failed signal cooldown.",
    ) -> Cooldown:
        return self.add_stopout_cooldown(
            symbol=symbol,
            strategy_id=strategy_id,
            now=now,
            minutes=minutes,
            reason=reason,
        )

    def is_blocked(
        self,
        *,
        symbol: str,
        strategy_id: str,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        now = now or datetime.now(UTC)
        cooldown = self._cooldowns.get((symbol, strategy_id))
        if cooldown and cooldown.cooldown_until > now:
            return True, cooldown.reason
        return False, "No active strategy cooldown."

