from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.enums import DecisionOutcome, DecisionType, MarketRegime
from trading_system.app.features.calculations import LiquidityGates, check_liquidity
from trading_system.app.strategies.cooldowns import StrategyCooldownBook
from trading_system.app.strategies.registry import StrategyRegistryService


SCANNER_RULE_VERSION = "vwap_reclaim_scanner_v1"
STRATEGY_ID = "VWAP_RECLAIM"


@dataclass(frozen=True)
class VwapReclaimSnapshot:
    symbol: str
    timestamp: datetime
    price: float
    previous_price: float
    vwap: float
    previous_vwap: float
    relative_volume: float
    average_volume: float
    dollar_volume: float
    spread_bps: float
    market_regime: MarketRegime
    has_catalyst: bool = False
    strong_relative_strength: bool = False


@dataclass(frozen=True)
class ScannerDecision:
    accepted: bool
    symbol: str
    strategy_id: str
    score: float
    reason: str
    rule_version: str = SCANNER_RULE_VERSION


class VwapReclaimScanner:
    def __init__(
        self,
        *,
        liquidity_gates: LiquidityGates,
        strategy_registry: StrategyRegistryService,
        cooldowns: StrategyCooldownBook | None = None,
        decision_logger: InMemoryDecisionLogger | None = None,
    ) -> None:
        self.liquidity_gates = liquidity_gates
        self.strategy_registry = strategy_registry
        self.cooldowns = cooldowns or StrategyCooldownBook()
        self.decision_logger = decision_logger or InMemoryDecisionLogger()

    def scan(self, snapshot: VwapReclaimSnapshot) -> ScannerDecision:
        allowed, reason = self.strategy_registry.can_generate_research_signal(STRATEGY_ID)
        if not allowed:
            return self._reject(snapshot, reason)

        blocked, cooldown_reason = self.cooldowns.is_blocked(
            symbol=snapshot.symbol, strategy_id=STRATEGY_ID, now=snapshot.timestamp
        )
        if blocked:
            return self._reject(snapshot, cooldown_reason)

        liquidity = check_liquidity(
            price=snapshot.price,
            average_volume=snapshot.average_volume,
            dollar_volume=snapshot.dollar_volume,
            spread_bps=snapshot.spread_bps,
            gates=self.liquidity_gates,
        )
        if not liquidity.passed:
            return self._reject(snapshot, liquidity.reason)

        if snapshot.market_regime == MarketRegime.RISK_OFF:
            return self._reject(snapshot, "Market regime is RISK_OFF.")
        if not (snapshot.previous_price < snapshot.previous_vwap and snapshot.price > snapshot.vwap):
            return self._reject(snapshot, "Price did not reclaim VWAP from below.")
        if snapshot.relative_volume <= 1.5:
            return self._reject(snapshot, "Relative volume must be above 1.5.")
        if not (snapshot.has_catalyst or snapshot.strong_relative_strength):
            return self._reject(snapshot, "Setup requires catalyst or strong relative strength.")

        score = min(
            100.0,
            55.0
            + min(snapshot.relative_volume, 4.0) * 7.5
            + (10.0 if snapshot.has_catalyst else 0.0)
            + (7.5 if snapshot.strong_relative_strength else 0.0),
        )
        decision = ScannerDecision(
            accepted=True,
            symbol=snapshot.symbol,
            strategy_id=STRATEGY_ID,
            score=score,
            reason="VWAP reclaim setup accepted.",
        )
        self.decision_logger.record_simple(
            DecisionType.SCANNER,
            DecisionOutcome.APPROVED,
            decision.reason,
            entity_id=snapshot.symbol,
            strategy_id=STRATEGY_ID,
            rule_version=SCANNER_RULE_VERSION,
        )
        return decision

    def _reject(self, snapshot: VwapReclaimSnapshot, reason: str) -> ScannerDecision:
        decision = ScannerDecision(
            accepted=False,
            symbol=snapshot.symbol,
            strategy_id=STRATEGY_ID,
            score=0.0,
            reason=reason,
        )
        self.decision_logger.record_simple(
            DecisionType.SCANNER,
            DecisionOutcome.REJECTED,
            reason,
            entity_id=snapshot.symbol,
            strategy_id=STRATEGY_ID,
            rule_version=SCANNER_RULE_VERSION,
        )
        return decision

