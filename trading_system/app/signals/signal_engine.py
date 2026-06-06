from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_system.app.audit.logger import InMemoryDecisionLogger
from trading_system.app.core.enums import DecisionOutcome, DecisionType, Direction, SignalStatus, TradeType
from trading_system.app.scanners.vwap_reclaim import ScannerDecision
from trading_system.app.signals.idempotency import IdempotencyRegistry, build_idempotency_key


SIGNAL_RULE_VERSION = "signal_engine_v1"


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    strategy_id: str
    strategy_version: str
    trade_type: TradeType
    direction: Direction
    entry_zone: tuple[float, float]
    stop_loss: float
    target_1: float
    target_2: float | None
    risk_reward: float
    confidence_score: float
    time_horizon: str
    invalidation: str
    source_timestamp: datetime
    idempotency_key: str
    status: SignalStatus = SignalStatus.CANDIDATE
    rule_version: str = SIGNAL_RULE_VERSION


class SignalEngine:
    def __init__(
        self,
        *,
        idempotency_registry: IdempotencyRegistry | None = None,
        decision_logger: InMemoryDecisionLogger | None = None,
    ) -> None:
        self.idempotency_registry = idempotency_registry or IdempotencyRegistry()
        self.decision_logger = decision_logger or InMemoryDecisionLogger()

    def create_vwap_reclaim_signal(
        self,
        *,
        scanner_decision: ScannerDecision,
        source_timestamp: datetime,
        price: float,
        stop_loss: float,
        strategy_version: str = "v1",
    ) -> TradeSignal:
        if not scanner_decision.accepted:
            self.decision_logger.record_simple(
                DecisionType.SIGNAL,
                DecisionOutcome.REJECTED,
                f"Scanner rejected candidate: {scanner_decision.reason}",
                entity_id=scanner_decision.symbol,
                strategy_id=scanner_decision.strategy_id,
                rule_version=SIGNAL_RULE_VERSION,
            )
            raise ValueError(f"Cannot create signal from rejected scanner result: {scanner_decision.reason}")
        if stop_loss >= price:
            raise ValueError("Long signal stop loss must be below entry price.")

        risk_per_share = price - stop_loss
        target_1 = price + (risk_per_share * 2)
        target_2 = price + (risk_per_share * 3)
        key = build_idempotency_key(
            namespace="signal",
            symbol=scanner_decision.symbol,
            strategy_id=scanner_decision.strategy_id,
            source_timestamp=source_timestamp,
            direction=Direction.LONG.value,
        )
        self.idempotency_registry.reserve(key)
        signal = TradeSignal(
            symbol=scanner_decision.symbol,
            strategy_id=scanner_decision.strategy_id,
            strategy_version=strategy_version,
            trade_type=TradeType.DAY_TRADE,
            direction=Direction.LONG,
            entry_zone=(price, price * 1.001),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            risk_reward=2.0,
            confidence_score=scanner_decision.score,
            time_horizon="intraday",
            invalidation="Loss of VWAP with rising sell volume.",
            source_timestamp=source_timestamp,
            idempotency_key=key,
        )
        self.decision_logger.record_simple(
            DecisionType.SIGNAL,
            DecisionOutcome.APPROVED,
            "Signal created with idempotency key.",
            entity_id=key,
            strategy_id=scanner_decision.strategy_id,
            rule_version=SIGNAL_RULE_VERSION,
        )
        return signal

