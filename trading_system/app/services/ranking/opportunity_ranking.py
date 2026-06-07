from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import ProviderHealthStatus
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict
from trading_system.app.scanners.production_scanners import (
    CATALYST_REQUIRED_STRATEGY_IDS,
    DAILY_DATA_FRESHNESS_SECONDS,
    PRODUCTION_DATA_PROVIDER,
    SCANNER_APPROVED_STATUSES,
)

RANKING_RULE_VERSION = "opportunity_ranking_v1"

COMPONENT_WEIGHTS: dict[str, float] = {
    "scanner": 30.0,
    "freshness": 15.0,
    "provider": 10.0,
    "regime": 15.0,
    "catalyst": 10.0,
    "relative_strength": 10.0,
    "liquidity": 10.0,
    "spread": 5.0,
}


class OpportunityGrade(str, Enum):
    A_PLUS = "A_PLUS"
    A = "A"
    B = "B"
    WATCH = "WATCH"
    REJECT = "REJECT"


@dataclass(frozen=True)
class RankingInputs:
    scanner_result_id: str
    symbol: str
    strategy_id: str
    scanner_name: str
    scanner_score: float
    strategy_status: str | None
    allowed_regimes: frozenset[str]
    cooldown_active: bool
    cooldown_until: str | None
    cooldown_reason: str | None
    provider: str | None
    provider_health_status: str | None
    provider_health_reliability: float | None
    provider_health_timestamp: datetime | None
    latest_data_timestamp: datetime | None
    timeframe: str
    market_regime: str | None
    regime_confidence: float | None
    regime_timestamp: datetime | None
    catalyst_id: str | None
    catalyst_materiality_score: float | None
    relative_strength_20d: float | None
    liquidity_score: float | None
    spread_score: float | None
    now: datetime


@dataclass(frozen=True)
class OpportunityRankingResult:
    scanner_result_id: str
    symbol: str
    strategy_id: str
    scanner_name: str
    opportunity_score: float
    grade: OpportunityGrade
    reasons: list[str]
    blocked_reason: str | None
    ranking_rule_version: str = RANKING_RULE_VERSION


def compute_opportunity_ranking(
    inputs: RankingInputs,
    settings: Settings,
) -> OpportunityRankingResult:
    blocked_reason = _hard_block_reason(inputs, settings)
    if blocked_reason:
        return OpportunityRankingResult(
            scanner_result_id=inputs.scanner_result_id,
            symbol=inputs.symbol,
            strategy_id=inputs.strategy_id,
            scanner_name=inputs.scanner_name,
            opportunity_score=0.0,
            grade=OpportunityGrade.REJECT,
            reasons=[],
            blocked_reason=blocked_reason,
        )

    component_scores, reasons = _score_components(inputs, settings)
    opportunity_score = round(
        sum(component_scores[name] * COMPONENT_WEIGHTS[name] / 100.0 for name in COMPONENT_WEIGHTS),
        2,
    )
    return OpportunityRankingResult(
        scanner_result_id=inputs.scanner_result_id,
        symbol=inputs.symbol,
        strategy_id=inputs.strategy_id,
        scanner_name=inputs.scanner_name,
        opportunity_score=opportunity_score,
        grade=_grade_from_score(opportunity_score),
        reasons=reasons,
        blocked_reason=None,
    )


class OpportunityRankingService:
    def __init__(self, repository: TradingRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def rank_recent_accepted(
        self,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[OpportunityRankingResult]:
        now = _as_utc(now)
        rows = self.repository.session.scalars(
            select(models.ScannerResult)
            .where(models.ScannerResult.accepted.is_(True))
            .order_by(desc(models.ScannerResult.created_at))
            .limit(limit)
        ).all()
        ranked = [self.rank_scanner_result(row, now) for row in rows]
        return sorted(ranked, key=lambda item: item.opportunity_score, reverse=True)

    def rank_scanner_result(
        self,
        scanner_result: models.ScannerResult,
        now: datetime | None = None,
    ) -> OpportunityRankingResult:
        now = _as_utc(now)
        inputs = self._build_ranking_inputs(scanner_result, now)
        return compute_opportunity_ranking(inputs, self.settings)

    def _build_ranking_inputs(
        self,
        scanner_result: models.ScannerResult,
        now: datetime,
    ) -> RankingInputs:
        payload = scanner_result.payload if isinstance(scanner_result.payload, dict) else {}
        preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}

        strategy_id = scanner_result.strategy_id or scanner_result.scanner_name
        strategy = preflight.get("strategy") if isinstance(preflight.get("strategy"), dict) else {}
        provider_health = (
            preflight.get("provider_health") if isinstance(preflight.get("provider_health"), dict) else {}
        )
        regime = preflight.get("regime") if isinstance(preflight.get("regime"), dict) else {}
        cooldown = preflight.get("cooldown") if isinstance(preflight.get("cooldown"), dict) else None

        catalyst_id = payload.get("catalyst_id")
        catalyst_materiality_score = None
        if catalyst_id:
            catalyst = self.repository.session.get(models.Catalyst, catalyst_id)
            if catalyst is not None:
                catalyst_materiality_score = float(catalyst.materiality_score)

        intraday = self._latest_intraday_features(scanner_result.symbol)

        return RankingInputs(
            scanner_result_id=scanner_result.id,
            symbol=scanner_result.symbol,
            strategy_id=strategy_id,
            scanner_name=scanner_result.scanner_name,
            scanner_score=float(scanner_result.score or 0.0),
            strategy_status=strategy.get("status"),
            allowed_regimes=frozenset(strategy.get("allowed_regimes") or []),
            cooldown_active=cooldown is not None,
            cooldown_until=str(cooldown.get("cooldown_until")) if cooldown else None,
            cooldown_reason=str(cooldown.get("reason")) if cooldown and cooldown.get("reason") else None,
            provider=preflight.get("provider"),
            provider_health_status=provider_health.get("status"),
            provider_health_reliability=(
                float(provider_health["reliability_score"])
                if provider_health.get("reliability_score") is not None
                else None
            ),
            provider_health_timestamp=_parse_timestamp(provider_health.get("source_timestamp")),
            latest_data_timestamp=_parse_timestamp(preflight.get("latest_data_timestamp")),
            timeframe=str(preflight.get("timeframe") or "1Min"),
            market_regime=regime.get("market_regime"),
            regime_confidence=(
                float(regime["confidence"]) if regime.get("confidence") is not None else None
            ),
            regime_timestamp=_parse_timestamp(regime.get("source_timestamp")),
            catalyst_id=str(catalyst_id) if catalyst_id else None,
            catalyst_materiality_score=catalyst_materiality_score,
            relative_strength_20d=(
                float(payload["relative_strength_20d"])
                if payload.get("relative_strength_20d") is not None
                else None
            ),
            liquidity_score=(
                float(intraday.liquidity_score)
                if intraday and intraday.liquidity_score is not None
                else None
            ),
            spread_score=(
                float(intraday.spread_score)
                if intraday and intraday.spread_score is not None
                else None
            ),
            now=now,
        )

    def _latest_intraday_features(self, symbol: str) -> models.FeatureIntraday | None:
        return self.repository.session.scalar(
            select(models.FeatureIntraday)
            .where(models.FeatureIntraday.symbol == symbol.upper())
            .order_by(desc(models.FeatureIntraday.created_at))
            .limit(1)
        )


def build_preflight_payload(
    repository: TradingRepository,
    *,
    symbol: str,
    strategy_id: str,
    timeframe: str,
    latest_data_timestamp: datetime,
    provider: str = PRODUCTION_DATA_PROVIDER,
    cooldown: models.StrategyCooldown | None = None,
) -> dict[str, Any]:
    strategy = repository.session.scalar(
        select(models.StrategyRegistry)
        .where(models.StrategyRegistry.strategy_id == strategy_id)
        .order_by(desc(models.StrategyRegistry.created_at))
        .limit(1)
    )
    provider_health = repository.latest_provider_health_for(PRODUCTION_DATA_PROVIDER)
    regime = repository.session.scalar(
        select(models.MarketRegimeSnapshot)
        .order_by(desc(models.MarketRegimeSnapshot.created_at))
        .limit(1)
    )
    return {
        "symbol": symbol,
        "strategy": model_to_dict(strategy) if strategy else None,
        "provider": provider,
        "provider_health": model_to_dict(provider_health) if provider_health else None,
        "timeframe": timeframe,
        "latest_data_timestamp": latest_data_timestamp.isoformat(),
        "regime": model_to_dict(regime) if regime else None,
        "cooldown": model_to_dict(cooldown) if cooldown else None,
    }


def _hard_block_reason(inputs: RankingInputs, settings: Settings) -> str | None:
    if inputs.strategy_status not in SCANNER_APPROVED_STATUSES:
        return (
            f"Strategy approval status {inputs.strategy_status} is not allowed for opportunity ranking."
        )
    if inputs.cooldown_active:
        reason = inputs.cooldown_reason or "cooldown active"
        return f"Strategy cooldown active until {inputs.cooldown_until}: {reason}"
    if not inputs.provider_health_status:
        return "Provider health is missing."
    if inputs.provider_health_status != ProviderHealthStatus.HEALTHY.value:
        return f"Provider health is {inputs.provider_health_status}."
    if not _timestamp_fresh(
        inputs.provider_health_timestamp,
        max_age_seconds=settings.provider_health_max_age_seconds,
        now=inputs.now,
    ):
        return "Provider health is stale."
    if inputs.provider != PRODUCTION_DATA_PROVIDER:
        return "Production ranking requires fresh Alpaca market data provider."
    if inputs.latest_data_timestamp is None:
        return "Market data timestamp is missing."
    if not _timestamp_fresh(
        inputs.latest_data_timestamp,
        max_age_seconds=_freshness_seconds_for_timeframe(inputs.timeframe, settings),
        now=inputs.now,
    ):
        return "Market data is stale for scanner timeframe."
    if not inputs.market_regime:
        return "Market regime snapshot is missing."
    if not _timestamp_fresh(
        inputs.regime_timestamp,
        max_age_seconds=max(settings.scheduler_regime_seconds * 3, settings.bar_freshness_max_seconds),
        now=inputs.now,
    ):
        return "Market regime snapshot is stale."
    if inputs.allowed_regimes and inputs.market_regime not in inputs.allowed_regimes:
        return (
            f"Market regime {inputs.market_regime} is not allowed for strategy {inputs.strategy_id}."
        )
    if inputs.strategy_id in CATALYST_REQUIRED_STRATEGY_IDS and not inputs.catalyst_id:
        return "Required catalyst is missing for catalyst-dependent strategy."
    return None


def _score_components(
    inputs: RankingInputs,
    settings: Settings,
) -> tuple[dict[str, float], list[str]]:
    reasons: list[str] = []
    scores: dict[str, float] = {}

    scanner_component = _clamp(inputs.scanner_score)
    scores["scanner"] = scanner_component
    reasons.append(f"Scanner score {scanner_component:.1f}")

    max_age = _freshness_seconds_for_timeframe(inputs.timeframe, settings)
    age_seconds = max(
        0.0,
        (inputs.now - inputs.latest_data_timestamp).total_seconds()
        if inputs.latest_data_timestamp
        else float(max_age),
    )
    freshness_ratio = 1.0 - min(1.0, age_seconds / max_age)
    freshness_component = _clamp(freshness_ratio * 100.0)
    scores["freshness"] = freshness_component
    reasons.append(f"Data freshness score {freshness_component:.1f}")

    provider_component = _clamp(inputs.provider_health_reliability or 100.0)
    scores["provider"] = provider_component
    reasons.append(f"Provider reliability {provider_component:.1f}")

    regime_component = _clamp(inputs.regime_confidence or 0.0)
    scores["regime"] = regime_component
    reasons.append(f"Regime confidence {regime_component:.1f}")

    if inputs.catalyst_materiality_score is not None:
        catalyst_component = _clamp(inputs.catalyst_materiality_score)
    elif inputs.strategy_id in CATALYST_REQUIRED_STRATEGY_IDS:
        catalyst_component = 0.0
    else:
        catalyst_component = 50.0
    scores["catalyst"] = catalyst_component
    reasons.append(f"Catalyst score {catalyst_component:.1f}")

    rs_value = inputs.relative_strength_20d or 0.0
    relative_strength_component = _clamp(min(100.0, max(0.0, rs_value * 20.0)))
    scores["relative_strength"] = relative_strength_component
    reasons.append(f"Relative strength score {relative_strength_component:.1f}")

    liquidity_component = _clamp(inputs.liquidity_score or 0.0)
    spread_component = _clamp(inputs.spread_score or 0.0)
    scores["liquidity"] = liquidity_component
    scores["spread"] = spread_component
    reasons.append(f"Liquidity score {liquidity_component:.1f}")
    reasons.append(f"Spread score {spread_component:.1f}")

    return scores, reasons


def _grade_from_score(score: float) -> OpportunityGrade:
    if score >= 88:
        return OpportunityGrade.A_PLUS
    if score >= 78:
        return OpportunityGrade.A
    if score >= 65:
        return OpportunityGrade.B
    if score >= 50:
        return OpportunityGrade.WATCH
    return OpportunityGrade.REJECT


def _freshness_seconds_for_timeframe(timeframe: str, settings: Settings) -> int:
    if timeframe == "1D":
        return DAILY_DATA_FRESHNESS_SECONDS
    return settings.bar_freshness_max_seconds


def _timestamp_fresh(timestamp: datetime | None, *, max_age_seconds: int, now: datetime) -> bool:
    if not timestamp:
        return False
    return timestamp >= now - timedelta(seconds=max_age_seconds)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return _as_utc(datetime.fromisoformat(normalized))
        except ValueError:
            return None
    return None


def _as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))
