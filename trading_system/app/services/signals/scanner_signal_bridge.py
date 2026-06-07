from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import DecisionOutcome, DecisionType, Direction, SignalStatus, TradeType
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.scanners.production_scanners import SCANNER_APPROVED_STATUSES
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityGrade,
    OpportunityRankingResult,
    OpportunityRankingService,
)
from trading_system.app.signals.signal_engine import SIGNAL_RULE_VERSION, TradeSignal

if TYPE_CHECKING:
    from trading_system.app.services.replay.decision_snapshot_service import DecisionSnapshotService

BRIDGE_RULE_VERSION = "scanner_signal_bridge_v1"
ALLOWED_SIGNAL_GRADES = frozenset({OpportunityGrade.A_PLUS, OpportunityGrade.A})

DEFAULT_INVALIDATION_BY_STRATEGY: dict[str, str] = {
    "VWAP_RECLAIM": "Loss of VWAP with rising sell volume.",
    "OPENING_RANGE_BREAKOUT": "Price falls back below opening range high on volume.",
    "RELATIVE_STRENGTH": "Relative strength deteriorates below threshold.",
    "NEWS_MOMENTUM": "News catalyst momentum fades with adverse price action.",
    "CATALYST_RUN_UP": "Catalyst run-up structure breaks down.",
    "POST_EARNINGS_CONTINUATION": "Post-earnings continuation structure fails.",
    "SECTOR_LEADERSHIP": "Sector leadership trend score weakens materially.",
}


@dataclass(frozen=True)
class ScannerBridgeSignal:
    symbol: str
    strategy_id: str
    strategy_version: str
    scanner_result_id: str
    trade_type: TradeType
    direction: Direction
    entry_zone: tuple[float, float]
    stop_loss: float
    target_1: float
    target_2: float | None
    risk_reward: float
    confidence_score: float
    regime_reference: str | None
    catalyst_reference: str | None
    invalidation_reason: str
    idempotency_key: str
    time_horizon: str
    source_timestamp: datetime
    rule_version: str = BRIDGE_RULE_VERSION

    def to_trade_signal(self) -> TradeSignal:
        return TradeSignal(
            symbol=self.symbol,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            trade_type=self.trade_type,
            direction=self.direction,
            entry_zone=self.entry_zone,
            stop_loss=self.stop_loss,
            target_1=self.target_1,
            target_2=self.target_2,
            risk_reward=self.risk_reward,
            confidence_score=self.confidence_score,
            time_horizon=self.time_horizon,
            invalidation=self.invalidation_reason,
            source_timestamp=self.source_timestamp,
            idempotency_key=self.idempotency_key,
            status=SignalStatus.CANDIDATE,
            rule_version=SIGNAL_RULE_VERSION,
        )


@dataclass(frozen=True)
class ScannerBridgeResult:
    created: bool
    signal: ScannerBridgeSignal | None
    signal_id: str | None
    blocked_reason: str | None
    ranking: OpportunityRankingResult | None = None


class ScannerSignalBridgeService:
    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings,
        *,
        ranking_service: OpportunityRankingService | None = None,
        snapshot_service: DecisionSnapshotService | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.ranking_service = ranking_service or OpportunityRankingService(repository, settings)
        self._snapshot_service = snapshot_service

    @property
    def snapshot_service(self):
        from trading_system.app.services.replay.decision_snapshot_service import DecisionSnapshotService

        if self._snapshot_service is None:
            self._snapshot_service = DecisionSnapshotService(self.repository)
        return self._snapshot_service

    def try_create_signal(
        self,
        scanner_result_id: str,
        *,
        ranking: OpportunityRankingResult | None = None,
        now: datetime | None = None,
    ) -> ScannerBridgeResult:
        now = _as_utc(now)
        scanner_result = self.repository.session.get(models.ScannerResult, scanner_result_id)
        if scanner_result is None:
            return ScannerBridgeResult(
                created=False,
                signal=None,
                signal_id=None,
                blocked_reason=f"Unknown scanner result: {scanner_result_id}",
            )

        resolved_ranking = ranking or self.ranking_service.rank_scanner_result(scanner_result, now)
        self.snapshot_service.capture_scanner_result(scanner_result, source_timestamp=now)
        self.snapshot_service.capture_opportunity_ranking(
            scanner_result,
            resolved_ranking,
            source_timestamp=now,
        )
        blocked_reason = self._bridge_block_reason(scanner_result, resolved_ranking, now)
        if blocked_reason:
            self._record_rejection(scanner_result, blocked_reason, resolved_ranking, now)
            self.snapshot_service.capture_signal_creation(
                scanner_result,
                resolved_ranking,
                created=False,
                blocked_reason=blocked_reason,
                source_timestamp=now,
            )
            return ScannerBridgeResult(
                created=False,
                signal=None,
                signal_id=None,
                blocked_reason=blocked_reason,
                ranking=resolved_ranking,
            )

        bridge_signal = self._build_bridge_signal(scanner_result, resolved_ranking)
        if self._idempotency_exists(bridge_signal.idempotency_key):
            reason = f"Duplicate idempotency key rejected: {bridge_signal.idempotency_key}"
            self._record_rejection(scanner_result, reason, resolved_ranking, now)
            self.snapshot_service.capture_signal_creation(
                scanner_result,
                resolved_ranking,
                created=False,
                bridge_signal=bridge_signal,
                blocked_reason=reason,
                source_timestamp=now,
            )
            return ScannerBridgeResult(
                created=False,
                signal=None,
                signal_id=None,
                blocked_reason=reason,
                ranking=resolved_ranking,
            )

        trade_signal = bridge_signal.to_trade_signal()
        signal_row = self.repository.store_signal(trade_signal)
        self.repository.store_signal_version(
            signal_id=signal_row.id,
            version=BRIDGE_RULE_VERSION,
            change_reason="Signal created from ranked scanner opportunity.",
            payload=_bridge_payload(bridge_signal, resolved_ranking),
            source_timestamp=bridge_signal.source_timestamp,
        )
        self.repository.store_decision_log(
            decision_type=DecisionType.SIGNAL,
            outcome=DecisionOutcome.APPROVED,
            entity_type="signal",
            entity_id=signal_row.id,
            strategy_id=bridge_signal.strategy_id,
            rule_version=BRIDGE_RULE_VERSION,
            reason="Scanner signal bridge created candidate signal.",
            payload={
                "scanner_result_id": scanner_result.id,
                "idempotency_key": bridge_signal.idempotency_key,
                "opportunity_grade": resolved_ranking.grade.value,
                "opportunity_score": resolved_ranking.opportunity_score,
            },
            source_timestamp=bridge_signal.source_timestamp,
        )
        self.snapshot_service.capture_signal_creation(
            scanner_result,
            resolved_ranking,
            created=True,
            signal_id=signal_row.id,
            bridge_signal=bridge_signal,
            source_timestamp=bridge_signal.source_timestamp,
        )
        return ScannerBridgeResult(
            created=True,
            signal=bridge_signal,
            signal_id=signal_row.id,
            blocked_reason=None,
            ranking=resolved_ranking,
        )

    def _bridge_block_reason(
        self,
        scanner_result: models.ScannerResult,
        ranking: OpportunityRankingResult,
        now: datetime,
    ) -> str | None:
        if not scanner_result.accepted:
            return f"Scanner result rejected: {scanner_result.reason}"

        strategy_id = scanner_result.strategy_id or scanner_result.scanner_name
        strategy = self._latest_strategy(strategy_id)
        if not strategy:
            return f"Strategy registry record missing for {strategy_id}."
        if strategy.status not in SCANNER_APPROVED_STATUSES:
            return (
                f"Strategy approval status {strategy.status} is not allowed for signal creation."
            )

        if ranking.blocked_reason:
            return ranking.blocked_reason

        if ranking.grade not in ALLOWED_SIGNAL_GRADES:
            return (
                f"Opportunity grade {ranking.grade.value} is below bridge threshold "
                f"(requires A_PLUS or A)."
            )

        cooldown = self.repository.active_strategy_cooldown(
            symbol=scanner_result.symbol,
            strategy_id=strategy_id,
            now=now,
        )
        if cooldown:
            return (
                f"Strategy cooldown active until {cooldown.cooldown_until.isoformat()}: "
                f"{cooldown.reason}"
            )

        idempotency_key = build_scanner_bridge_idempotency_key(
            scanner_result_id=scanner_result.id,
            symbol=scanner_result.symbol,
            strategy_id=strategy_id,
        )
        if self._idempotency_exists(idempotency_key):
            return f"Duplicate idempotency key rejected: {idempotency_key}"
        return None

    def _build_bridge_signal(
        self,
        scanner_result: models.ScannerResult,
        ranking: OpportunityRankingResult,
    ) -> ScannerBridgeSignal:
        strategy_id = scanner_result.strategy_id or scanner_result.scanner_name
        strategy = self._latest_strategy(strategy_id)
        if strategy is None:
            raise ValueError(f"Strategy registry record missing for {strategy_id}.")

        payload = scanner_result.payload if isinstance(scanner_result.payload, dict) else {}
        preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
        regime = preflight.get("regime") if isinstance(preflight.get("regime"), dict) else {}

        trade_type = TradeType(strategy.trade_type)
        direction = Direction.LONG
        price, stop_loss, invalidation = _derive_trade_levels(
            strategy_id=strategy_id,
            payload=payload,
            scanner_reason=scanner_result.reason,
            repository=self.repository,
            symbol=scanner_result.symbol,
        )
        risk_per_share = price - stop_loss
        target_1 = price + (risk_per_share * 2)
        target_2 = price + (risk_per_share * 3)

        catalyst_reference = str(payload["catalyst_id"]) if payload.get("catalyst_id") else None
        regime_reference = (
            str(regime.get("id"))
            if regime.get("id")
            else str(regime.get("market_regime"))
            if regime.get("market_regime")
            else None
        )

        return ScannerBridgeSignal(
            symbol=scanner_result.symbol,
            strategy_id=strategy_id,
            strategy_version=strategy.version,
            scanner_result_id=scanner_result.id,
            trade_type=trade_type,
            direction=direction,
            entry_zone=(price, round(price * 1.001, 4)),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            risk_reward=2.0,
            confidence_score=ranking.opportunity_score,
            regime_reference=regime_reference,
            catalyst_reference=catalyst_reference,
            invalidation_reason=invalidation,
            idempotency_key=build_scanner_bridge_idempotency_key(
                scanner_result_id=scanner_result.id,
                symbol=scanner_result.symbol,
                strategy_id=strategy_id,
            ),
            time_horizon=_time_horizon_for_trade_type(trade_type),
            source_timestamp=scanner_result.source_timestamp,
        )

    def _latest_strategy(self, strategy_id: str) -> models.StrategyRegistry | None:
        return self.repository.session.scalar(
            select(models.StrategyRegistry)
            .where(models.StrategyRegistry.strategy_id == strategy_id)
            .order_by(desc(models.StrategyRegistry.created_at))
            .limit(1)
        )

    def _idempotency_exists(self, idempotency_key: str) -> bool:
        existing = self.repository.session.scalar(
            select(models.Signal).where(models.Signal.idempotency_key == idempotency_key)
        )
        return existing is not None

    def _record_rejection(
        self,
        scanner_result: models.ScannerResult,
        reason: str,
        ranking: OpportunityRankingResult,
        now: datetime,
    ) -> None:
        self.repository.store_decision_log(
            decision_type=DecisionType.SIGNAL,
            outcome=DecisionOutcome.REJECTED,
            entity_type="scanner_result",
            entity_id=scanner_result.id,
            strategy_id=scanner_result.strategy_id or scanner_result.scanner_name,
            rule_version=BRIDGE_RULE_VERSION,
            reason=reason,
            payload={
                "scanner_result_id": scanner_result.id,
                "opportunity_grade": ranking.grade.value,
                "opportunity_score": ranking.opportunity_score,
                "blocked_reason": ranking.blocked_reason,
            },
            source_timestamp=now,
        )


def build_scanner_bridge_idempotency_key(
    *,
    scanner_result_id: str,
    symbol: str,
    strategy_id: str,
) -> str:
    raw = f"scanner_signal_bridge:{scanner_result_id}:{symbol.upper()}:{strategy_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _derive_trade_levels(
    *,
    strategy_id: str,
    payload: dict[str, Any],
    scanner_reason: str,
    repository: TradingRepository,
    symbol: str,
) -> tuple[float, float, str]:
    price = _extract_price(payload, repository, symbol)
    if price is None or price <= 0:
        raise ValueError("Cannot derive entry price from scanner payload or feature snapshot.")

    vwap = payload.get("latest_vwap")
    if vwap is not None:
        stop_loss = min(float(vwap), price * 0.995)
    elif payload.get("opening_high") is not None:
        stop_loss = float(payload["opening_high"]) * 0.995
    else:
        stop_loss = price * 0.98

    if stop_loss >= price:
        stop_loss = price * 0.99

    invalidation = DEFAULT_INVALIDATION_BY_STRATEGY.get(strategy_id)
    if not invalidation:
        invalidation = scanner_reason or "Setup conditions invalidated."
    return price, stop_loss, invalidation


def _extract_price(
    payload: dict[str, Any],
    repository: TradingRepository,
    symbol: str,
) -> float | None:
    for key in ("latest_close", "price"):
        value = payload.get(key)
        if value is not None:
            return float(value)

    intraday = repository.session.scalar(
        select(models.FeatureIntraday)
        .where(models.FeatureIntraday.symbol == symbol.upper())
        .order_by(desc(models.FeatureIntraday.created_at))
        .limit(1)
    )
    if intraday and intraday.price is not None:
        return float(intraday.price)
    return None


def _time_horizon_for_trade_type(trade_type: TradeType) -> str:
    if trade_type == TradeType.DAY_TRADE:
        return "intraday"
    if trade_type == TradeType.SWING:
        return "multi_day"
    return "quarter"


def _bridge_payload(
    bridge_signal: ScannerBridgeSignal,
    ranking: OpportunityRankingResult,
) -> dict[str, Any]:
    return {
        "bridge_rule_version": BRIDGE_RULE_VERSION,
        "scanner_result_id": bridge_signal.scanner_result_id,
        "regime_reference": bridge_signal.regime_reference,
        "catalyst_reference": bridge_signal.catalyst_reference,
        "invalidation_reason": bridge_signal.invalidation_reason,
        "idempotency_key": bridge_signal.idempotency_key,
        "opportunity_grade": ranking.grade.value,
        "opportunity_score": ranking.opportunity_score,
        "ranking_reasons": ranking.reasons,
    }


def _as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
