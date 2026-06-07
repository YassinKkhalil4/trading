from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from trading_system.app.core.enums import DecisionOutcome, DecisionType
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.risk.risk_engine import PortfolioState, RiskDecision, RISK_RULE_VERSION
from trading_system.app.services.ranking.opportunity_ranking import (
    OpportunityRankingResult,
    RANKING_RULE_VERSION,
)
from trading_system.app.signals.signal_engine import TradeSignal

if TYPE_CHECKING:
    from trading_system.app.services.portfolio.portfolio_engine import PortfolioDecision
    from trading_system.app.services.signals.scanner_signal_bridge import ScannerBridgeSignal

BRIDGE_RULE_VERSION = "scanner_signal_bridge_v1"

DECISION_SNAPSHOT_VERSION = "decision_snapshot_v1"

FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {
        "trade_outcome",
        "pnl",
        "realized_pnl",
        "fill_price",
        "exit_price",
        "trade_journal_id",
        "order_fill",
        "post_trade",
        "outcome_pnl",
        "trade_result",
    }
)

FEATURE_VALUE_KEYS = (
    "latest_close",
    "latest_vwap",
    "relative_strength_20d",
    "opening_high",
    "opening_low",
    "price",
    "liquidity_score",
    "spread_score",
    "relative_volume",
    "gap_pct",
    "atr",
)


class DecisionSnapshotStage(str, Enum):
    SCANNER_RESULT = "scanner_result"
    OPPORTUNITY_RANKING = "opportunity_ranking"
    SIGNAL_CREATION = "signal_creation"
    PORTFOLIO_DECISION = "portfolio_decision"
    RISK_DECISION = "risk_decision"


_STAGE_DECISION_TYPE: dict[DecisionSnapshotStage, DecisionType] = {
    DecisionSnapshotStage.SCANNER_RESULT: DecisionType.SCANNER,
    DecisionSnapshotStage.OPPORTUNITY_RANKING: DecisionType.SCANNER,
    DecisionSnapshotStage.SIGNAL_CREATION: DecisionType.SIGNAL,
    DecisionSnapshotStage.PORTFOLIO_DECISION: DecisionType.STRATEGY,
    DecisionSnapshotStage.RISK_DECISION: DecisionType.RISK,
}

_STAGE_RULE_VERSION: dict[DecisionSnapshotStage, str] = {
    DecisionSnapshotStage.SCANNER_RESULT: "production_scanners_v2",
    DecisionSnapshotStage.OPPORTUNITY_RANKING: RANKING_RULE_VERSION,
    DecisionSnapshotStage.SIGNAL_CREATION: BRIDGE_RULE_VERSION,
    DecisionSnapshotStage.PORTFOLIO_DECISION: "portfolio_engine_v1",
    DecisionSnapshotStage.RISK_DECISION: RISK_RULE_VERSION,
}


@dataclass(frozen=True)
class DecisionSnapshotRecord:
    stage: DecisionSnapshotStage
    timestamp: datetime
    symbol: str
    strategy: str
    feature_values: dict[str, Any]
    regime_state: dict[str, Any] | None
    catalyst_state: dict[str, Any] | None
    provider_health: dict[str, Any] | None
    data_freshness: dict[str, Any] | None
    reasons: list[str]
    decision_result: dict[str, Any]
    entity_refs: dict[str, Any]
    snapshot_version: str = DECISION_SNAPSHOT_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "stage": self.stage.value,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "strategy": self.strategy,
            "feature_values": self.feature_values,
            "regime_state": self.regime_state,
            "catalyst_state": self.catalyst_state,
            "provider_health": self.provider_health,
            "data_freshness": self.data_freshness,
            "reasons": self.reasons,
            "decision_result": self.decision_result,
            "entity_refs": self.entity_refs,
        }


class DecisionSnapshotService:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def capture_scanner_result(
        self,
        scanner_result: models.ScannerResult,
        *,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        context = _extract_scanner_context(scanner_result)
        record = DecisionSnapshotRecord(
            stage=DecisionSnapshotStage.SCANNER_RESULT,
            timestamp=_as_utc(source_timestamp or scanner_result.source_timestamp),
            symbol=scanner_result.symbol,
            strategy=scanner_result.strategy_id or scanner_result.scanner_name,
            feature_values=context["feature_values"],
            regime_state=context["regime_state"],
            catalyst_state=context["catalyst_state"],
            provider_health=context["provider_health"],
            data_freshness=context["data_freshness"],
            reasons=[scanner_result.reason],
            decision_result={
                "accepted": scanner_result.accepted,
                "score": scanner_result.score,
                "scanner_name": scanner_result.scanner_name,
                "scanner_rule_version": scanner_result.scanner_rule_version,
            },
            entity_refs={"scanner_result_id": scanner_result.id},
        )
        return self._persist(
            record,
            outcome=DecisionOutcome.APPROVED if scanner_result.accepted else DecisionOutcome.REJECTED,
            entity_id=scanner_result.id,
        )

    def capture_opportunity_ranking(
        self,
        scanner_result: models.ScannerResult,
        ranking: OpportunityRankingResult,
        *,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        context = _extract_scanner_context(scanner_result)
        record = DecisionSnapshotRecord(
            stage=DecisionSnapshotStage.OPPORTUNITY_RANKING,
            timestamp=_as_utc(source_timestamp or scanner_result.source_timestamp),
            symbol=ranking.symbol,
            strategy=ranking.strategy_id,
            feature_values=context["feature_values"],
            regime_state=context["regime_state"],
            catalyst_state=context["catalyst_state"],
            provider_health=context["provider_health"],
            data_freshness=context["data_freshness"],
            reasons=ranking.reasons,
            decision_result={
                "opportunity_score": ranking.opportunity_score,
                "grade": ranking.grade.value,
                "blocked_reason": ranking.blocked_reason,
                "ranking_rule_version": ranking.ranking_rule_version,
            },
            entity_refs={
                "scanner_result_id": scanner_result.id,
                "scanner_name": ranking.scanner_name,
            },
        )
        outcome = DecisionOutcome.REJECTED if ranking.blocked_reason else DecisionOutcome.APPROVED
        return self._persist(record, outcome=outcome, entity_id=scanner_result.id)

    def capture_signal_creation(
        self,
        scanner_result: models.ScannerResult,
        ranking: OpportunityRankingResult,
        *,
        created: bool,
        signal_id: str | None = None,
        bridge_signal: ScannerBridgeSignal | None = None,
        blocked_reason: str | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        context = _extract_scanner_context(scanner_result)
        reasons = [blocked_reason] if blocked_reason else ["Signal created from ranked scanner opportunity."]
        decision_result: dict[str, Any] = {
            "created": created,
            "blocked_reason": blocked_reason,
            "opportunity_grade": ranking.grade.value,
            "opportunity_score": ranking.opportunity_score,
            "bridge_rule_version": BRIDGE_RULE_VERSION,
        }
        if bridge_signal is not None:
            decision_result.update(
                {
                    "idempotency_key": bridge_signal.idempotency_key,
                    "entry_zone": list(bridge_signal.entry_zone),
                    "stop_loss": bridge_signal.stop_loss,
                    "target_1": bridge_signal.target_1,
                    "target_2": bridge_signal.target_2,
                    "confidence_score": bridge_signal.confidence_score,
                }
            )
        entity_refs: dict[str, Any] = {"scanner_result_id": scanner_result.id}
        if signal_id:
            entity_refs["signal_id"] = signal_id
        record = DecisionSnapshotRecord(
            stage=DecisionSnapshotStage.SIGNAL_CREATION,
            timestamp=_as_utc(source_timestamp or scanner_result.source_timestamp),
            symbol=scanner_result.symbol,
            strategy=scanner_result.strategy_id or scanner_result.scanner_name,
            feature_values=context["feature_values"],
            regime_state=context["regime_state"],
            catalyst_state=context["catalyst_state"],
            provider_health=context["provider_health"],
            data_freshness=context["data_freshness"],
            reasons=[reason for reason in reasons if reason],
            decision_result=decision_result,
            entity_refs=entity_refs,
        )
        outcome = DecisionOutcome.APPROVED if created else DecisionOutcome.REJECTED
        entity_id = signal_id or scanner_result.id
        return self._persist(record, outcome=outcome, entity_id=entity_id)

    def capture_portfolio_decision(
        self,
        signal: TradeSignal,
        decision: PortfolioDecision,
        *,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        record = DecisionSnapshotRecord(
            stage=DecisionSnapshotStage.PORTFOLIO_DECISION,
            timestamp=_as_utc(source_timestamp or signal.source_timestamp),
            symbol=signal.symbol,
            strategy=signal.strategy_id,
            feature_values=_feature_values_from_signal(signal),
            regime_state=None,
            catalyst_state=None,
            provider_health=None,
            data_freshness=None,
            reasons=list(decision.reasons),
            decision_result={
                "outcome": decision.outcome.value,
                "approved": decision.approved,
                "recommended_size_multiplier": decision.recommended_size_multiplier,
                "portfolio_rule_version": decision.portfolio_rule_version,
                "exposure_snapshot": {
                    "open_positions": decision.exposure_snapshot.open_positions,
                    "proposed_exposure_pct": decision.exposure_snapshot.proposed_exposure_pct,
                    "signal_sector": decision.exposure_snapshot.signal_sector,
                },
            },
            entity_refs={"signal_id": decision.signal_id},
        )
        outcome = DecisionOutcome.REJECTED if not decision.approved else DecisionOutcome.APPROVED
        return self._persist(record, outcome=outcome, entity_id=decision.signal_id)

    def capture_risk_decision(
        self,
        *,
        signal: TradeSignal,
        signal_id: str,
        portfolio_state: PortfolioState,
        risk_decision: RiskDecision,
        risk_context: dict[str, Any] | None = None,
        source_timestamp: datetime | None = None,
    ) -> models.DecisionLog:
        context = risk_context or {}
        record = DecisionSnapshotRecord(
            stage=DecisionSnapshotStage.RISK_DECISION,
            timestamp=_as_utc(source_timestamp or signal.source_timestamp),
            symbol=signal.symbol,
            strategy=signal.strategy_id,
            feature_values=_feature_values_from_signal(signal, context),
            regime_state=_regime_from_context(context),
            catalyst_state=_catalyst_from_context(context),
            provider_health=_provider_health_from_context(context),
            data_freshness=_data_freshness_from_context(context),
            reasons=[risk_decision.reason],
            decision_result={
                "approved": risk_decision.approved,
                "position_size": risk_decision.position_size,
                "risk_amount": risk_decision.risk_amount,
                "risk_rule_version": risk_decision.risk_rule_version,
                "portfolio_state": {
                    "account_equity": portfolio_state.account_equity,
                    "open_positions": portfolio_state.open_positions,
                    "daily_loss_pct": portfolio_state.daily_loss_pct,
                    "weekly_loss_pct": portfolio_state.weekly_loss_pct,
                    "sector_exposure_pct": portfolio_state.sector_exposure_pct,
                    "symbol_exposure_pct": portfolio_state.symbol_exposure_pct,
                    "strategy_exposure_pct": portfolio_state.strategy_exposure_pct,
                    "correlated_exposure_pct": portfolio_state.correlated_exposure_pct,
                    "overnight_exposure_pct": portfolio_state.overnight_exposure_pct,
                    "spread_bps": portfolio_state.spread_bps,
                    "expected_slippage_bps": portfolio_state.expected_slippage_bps,
                    "volatility_score": portfolio_state.volatility_score,
                    "kill_switch_active": portfolio_state.kill_switch_active,
                    "broker_sync_ok": portfolio_state.broker_sync_ok,
                },
            },
            entity_refs={"signal_id": signal_id},
        )
        outcome = DecisionOutcome.APPROVED if risk_decision.approved else DecisionOutcome.REJECTED
        return self._persist(record, outcome=outcome, entity_id=signal_id)

    def list_snapshots(
        self,
        *,
        stage: DecisionSnapshotStage | None = None,
        entity_id: str | None = None,
        limit: int = 50,
    ) -> list[models.DecisionLog]:
        return self.repository.list_decision_snapshots(
            stage=stage.value if stage else None,
            entity_id=entity_id,
            limit=limit,
        )

    def _persist(
        self,
        record: DecisionSnapshotRecord,
        *,
        outcome: DecisionOutcome,
        entity_id: str,
    ) -> models.DecisionLog:
        payload = record.to_payload()
        _assert_no_forbidden_keys(payload)
        return self.repository.store_decision_snapshot(
            stage=record.stage.value,
            decision_type=_STAGE_DECISION_TYPE[record.stage],
            outcome=outcome,
            symbol=record.symbol,
            strategy_id=record.strategy,
            entity_id=entity_id,
            rule_version=_STAGE_RULE_VERSION[record.stage],
            reason="; ".join(record.reasons) if record.reasons else "Decision snapshot captured.",
            payload=payload,
            source_timestamp=record.timestamp,
        )


def _extract_scanner_context(scanner_result: models.ScannerResult) -> dict[str, Any]:
    payload = scanner_result.payload if isinstance(scanner_result.payload, dict) else {}
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    regime = preflight.get("regime") if isinstance(preflight.get("regime"), dict) else None
    provider_health = (
        preflight.get("provider_health") if isinstance(preflight.get("provider_health"), dict) else None
    )

    feature_values: dict[str, Any] = {}
    for key in FEATURE_VALUE_KEYS:
        if payload.get(key) is not None:
            feature_values[key] = payload[key]

    catalyst_state = None
    catalyst_id = payload.get("catalyst_id")
    if catalyst_id:
        catalyst_state = {"catalyst_id": str(catalyst_id)}
        if payload.get("catalyst_materiality_score") is not None:
            catalyst_state["materiality_score"] = payload["catalyst_materiality_score"]

    data_freshness = {
        "timeframe": preflight.get("timeframe"),
        "latest_data_timestamp": preflight.get("latest_data_timestamp"),
        "provider": preflight.get("provider"),
    }

    return {
        "feature_values": feature_values,
        "regime_state": regime,
        "catalyst_state": catalyst_state,
        "provider_health": provider_health,
        "data_freshness": data_freshness,
    }


def _feature_values_from_signal(signal: TradeSignal, context: dict[str, Any] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "entry_price": signal.entry_zone[0],
        "stop_loss": signal.stop_loss,
        "target_1": signal.target_1,
        "confidence_score": signal.confidence_score,
        "risk_reward": signal.risk_reward,
    }
    if context:
        for key in FEATURE_VALUE_KEYS:
            if context.get(key) is not None:
                values[key] = context[key]
        volatility = context.get("volatility_score")
        if volatility is not None:
            values["volatility_score"] = volatility
    return values


def _regime_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    regime = context.get("regime_state")
    if isinstance(regime, dict):
        return regime
    return None


def _catalyst_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    catalyst = context.get("catalyst_state")
    if isinstance(catalyst, dict):
        return catalyst
    return None


def _provider_health_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    provider_health = context.get("provider_health")
    if isinstance(provider_health, dict):
        return provider_health
    return None


def _data_freshness_from_context(context: dict[str, Any]) -> dict[str, Any] | None:
    freshness = context.get("data_freshness")
    if isinstance(freshness, dict):
        return freshness
    latest = context.get("latest_data_timestamp")
    if latest is not None:
        return {"latest_data_timestamp": latest}
    return None


def _assert_no_forbidden_keys(payload: dict[str, Any]) -> None:
    for key in _collect_keys(payload):
        if key in FORBIDDEN_SNAPSHOT_KEYS:
            raise ValueError(f"Decision snapshot must not include future data key: {key}")


def _collect_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys.update(_collect_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_collect_keys(item))
    return keys


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
