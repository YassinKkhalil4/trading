from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.services.ranking.expectancy import ExpectancyService

ALPHA_SCORING_VERSION = "alpha_opportunity_scoring_v1"

COMPONENT_WEIGHTS: dict[str, float] = {
    "catalyst_quality": 20.0,
    "volume_anomaly": 15.0,
    "price_reaction": 15.0,
    "technical_structure": 15.0,
    "relative_strength": 10.0,
    "liquidity_spread": 10.0,
    "market_regime": 10.0,
    "historical_expectancy": 5.0,
}
HOSTILE_REGIMES = {"BEAR_TREND", "RISK_OFF", "HIGH_VOLATILITY", "MACRO_EVENT_RISK"}
BULLISH_REGIMES = {"BULL_TREND", "RISK_ON", "LOW_VOLATILITY", "EARNINGS_SEASON"}


@dataclass(frozen=True)
class AlphaScoreResult:
    opportunity_score_id: str
    scanner_result_id: str
    symbol: str
    strategy_id: str
    score: float
    grade: str
    component_scores: dict[str, float]
    penalties: list[dict[str, Any]]
    explanation: str
    created_signal: bool = False
    signal_id: str | None = None


class AlphaOpportunityScoringService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.session = repository.session
        self.settings = settings or get_settings()

    def score_recent_accepted(
        self,
        *,
        limit: int = 100,
        symbols: list[str] | None = None,
    ) -> list[AlphaScoreResult]:
        query = select(models.ScannerResult).where(models.ScannerResult.accepted.is_(True))
        if symbols:
            query = query.where(models.ScannerResult.symbol.in_([symbol.upper() for symbol in symbols]))
        rows = self.session.scalars(
            query.order_by(desc(models.ScannerResult.source_timestamp), desc(models.ScannerResult.created_at))
            .limit(limit)
        ).all()
        return [self.score_scanner_result(row) for row in rows]

    def score_scanner_result(self, scanner_result: models.ScannerResult) -> AlphaScoreResult:
        now = datetime.now(UTC)
        payload = scanner_result.payload if isinstance(scanner_result.payload, dict) else {}
        symbol = scanner_result.symbol.upper()
        strategy_id = scanner_result.strategy_id or scanner_result.scanner_name
        catalyst = self._latest_catalyst(symbol, now=now)
        news = self._latest_news(symbol, now=now)
        intraday = self.repository.latest_features_for(symbol)
        daily = self.repository.latest_daily_feature_for(symbol)
        universe = self.session.scalar(select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol))
        regime = self._latest_market_regime()
        expectancy = ExpectancyService(self.repository).load().match(
            strategy_id=strategy_id,
            symbol=symbol,
            regime=regime,
        )
        component_scores = {
            "catalyst_quality": self._score_catalyst(catalyst, news, now, payload),
            "volume_anomaly": self._score_volume(payload, intraday),
            "price_reaction": self._score_price_reaction(payload, daily),
            "technical_structure": self._score_technical_structure(payload, scanner_result),
            "relative_strength": self._score_relative_strength(payload),
            "liquidity_spread": self._score_liquidity_spread(payload, intraday, universe),
            "market_regime": self._score_market_regime(regime),
            "historical_expectancy": self._score_expectancy(expectancy.expectancy, expectancy.sample_size),
        }
        penalties = self._penalties(payload, regime, expectancy.expectancy, expectancy.sample_size)
        weighted = sum(component_scores[name] * COMPONENT_WEIGHTS[name] for name in COMPONENT_WEIGHTS)
        score = max(0.0, min(100.0, weighted / sum(COMPONENT_WEIGHTS.values())))
        for penalty in penalties:
            score -= float(penalty.get("points", 0.0))
        score = round(max(0.0, min(100.0, score)), 2)
        grade = grade_from_score(score)
        confidence_level = confidence_from_sample(expectancy.sample_size)
        risk_multiplier = suggested_risk_multiplier(
            grade=grade,
            expectancy=expectancy.expectancy,
            sample_size=expectancy.sample_size,
            regime=regime,
        )
        explanation = self._explain(scanner_result, component_scores, penalties, grade)
        components = [
            {
                "component_name": name,
                "raw_value": component_scores[name],
                "score": component_scores[name],
                "weight": COMPONENT_WEIGHTS[name],
                "explanation": _component_explanation(name, component_scores[name]),
            }
            for name in COMPONENT_WEIGHTS
        ]
        row = self.repository.store_opportunity_score(
            scanner_result_id=scanner_result.id,
            signal_id=None,
            symbol=symbol,
            strategy_id=strategy_id,
            setup_type=payload.get("setup_type") or scanner_result.scanner_name,
            score=score,
            grade=grade,
            component_scores=component_scores,
            components=components,
            penalties=penalties,
            explanation=explanation,
            expected_r=expectancy.avg_r,
            historical_win_rate=expectancy.win_rate,
            expectancy_sample_size=expectancy.sample_size,
            confidence_level=confidence_level,
            suggested_risk_multiplier=risk_multiplier,
            market_regime=regime,
            sector_regime=self._sector_regime(universe.sector if universe else None),
            catalyst_type=catalyst.catalyst_type if catalyst else payload.get("catalyst_type"),
            linked_news_id=news.id if news else None,
            payload={
                "scoring_version": ALPHA_SCORING_VERSION,
                "scanner_payload": payload,
                "expectancy_matched_on": expectancy.matched_on,
            },
            source_timestamp=now,
        )
        if grade in {"WATCH", "C"}:
            self.repository.store_alpha_rejection_reason(
                scanner_result_id=scanner_result.id,
                symbol=symbol,
                strategy_id=strategy_id,
                setup_type=payload.get("setup_type") or scanner_result.scanner_name,
                reason_code="LOW_ALPHA_SCORE",
                reason=f"Opportunity grade {grade} is watchlist-only; no trade signal should be created.",
                severity="WARNING",
                payload={"score": score, "grade": grade},
                source_timestamp=now,
            )
        return AlphaScoreResult(
            opportunity_score_id=row.id,
            scanner_result_id=scanner_result.id,
            symbol=symbol,
            strategy_id=strategy_id,
            score=score,
            grade=grade,
            component_scores=component_scores,
            penalties=penalties,
            explanation=explanation,
        )

    def _latest_catalyst(self, symbol: str, *, now: datetime) -> models.Catalyst | None:
        cutoff = now - timedelta(hours=48)
        return self.session.scalar(
            select(models.Catalyst)
            .where(models.Catalyst.symbol == symbol, models.Catalyst.source_timestamp >= cutoff)
            .order_by(desc(models.Catalyst.source_timestamp), desc(models.Catalyst.created_at))
            .limit(1)
        )

    def _latest_news(self, symbol: str, *, now: datetime) -> models.CleanNews | None:
        cutoff = now - timedelta(hours=48)
        return self.session.scalar(
            select(models.CleanNews)
            .where(models.CleanNews.symbol == symbol, models.CleanNews.source_timestamp >= cutoff)
            .order_by(desc(models.CleanNews.source_timestamp), desc(models.CleanNews.created_at))
            .limit(1)
        )

    def _latest_market_regime(self) -> str | None:
        row = self.session.scalar(
            select(models.MarketRegimeSnapshot)
            .order_by(desc(models.MarketRegimeSnapshot.source_timestamp), desc(models.MarketRegimeSnapshot.created_at))
            .limit(1)
        )
        return row.market_regime if row else None

    def _sector_regime(self, sector: str | None) -> str | None:
        if not sector:
            return None
        row = self.session.scalar(
            select(models.SectorStrengthSnapshot)
            .where(models.SectorStrengthSnapshot.sector == sector)
            .order_by(desc(models.SectorStrengthSnapshot.source_timestamp), desc(models.SectorStrengthSnapshot.created_at))
            .limit(1)
        )
        return row.regime if row else None

    def _score_catalyst(self, catalyst, news, now: datetime, payload: dict[str, Any]) -> float:
        materiality = float(catalyst.materiality_score) if catalyst else float(payload.get("catalyst_score") or 0.0)
        freshness = 0.0
        if catalyst and catalyst.source_timestamp:
            age_hours = max(0.0, (now - _as_utc(catalyst.source_timestamp)).total_seconds() / 3600)
            freshness = max(0.0, 100.0 - age_hours * 4.0)
        relevance = float(getattr(news, "relevance_score", 0.0) or 0.0) * 100 if news else 0.0
        confidence = float(getattr(news, "source_confidence_score", 0.0) or 0.0)
        return round(max(materiality, (materiality * 0.5 + freshness * 0.3 + relevance * 0.1 + confidence * 0.1)), 2)

    def _score_volume(self, payload: dict[str, Any], intraday) -> float:
        rv = _first_float(payload, ("relative_volume",), ("snapshot", "relative_volume"), ("request", "relative_volume"))
        if rv is None and intraday is not None:
            rv = intraday.relative_volume
        if rv is None:
            return 25.0
        return round(max(0.0, min(100.0, (float(rv) / 3.0) * 100.0)), 2)

    def _score_price_reaction(self, payload: dict[str, Any], daily) -> float:
        gap = _first_float(payload, ("gap_pct",), ("snapshot", "gap_pct"), ("request", "gap_pct"))
        if gap is None and daily is not None:
            gap = daily.gap_pct
        price_above_previous = bool(payload.get("price_above_previous_close")) or (gap is not None and gap > 0)
        reaction = abs(float(gap or 0.0)) * 8.0
        return round(max(0.0, min(100.0, reaction + (30.0 if price_above_previous else 0.0))), 2)

    def _score_technical_structure(self, payload: dict[str, Any], scanner_result: models.ScannerResult) -> float:
        score = float(scanner_result.score or 0.0) * 0.5
        if payload.get("vwap_reclaim") or payload.get("latest_close", 0) > payload.get("latest_vwap", float("inf")):
            score += 25.0
        if payload.get("opening_range_breakout") or payload.get("breakout_confirmed"):
            score += 20.0
        if payload.get("retest_hold"):
            score += 10.0
        return round(max(0.0, min(100.0, score)), 2)

    def _score_relative_strength(self, payload: dict[str, Any]) -> float:
        spy = _first_float(payload, ("relative_strength_20d",), ("stock_vs_spy_score",))
        sector = _first_float(payload, ("stock_vs_sector_score",))
        values = [value for value in (spy, sector) if value is not None]
        if not values:
            return 50.0
        return round(max(0.0, min(100.0, 50.0 + sum(values) / len(values) * 10.0)), 2)

    def _score_liquidity_spread(self, payload: dict[str, Any], intraday, universe) -> float:
        spread_bps = _first_float(payload, ("spread_bps",), ("snapshot", "spread_bps"), ("request", "spread_bps"))
        if spread_bps is None and intraday is not None:
            spread_bps = 100.0 - float(intraday.spread_score or 0.0)
        dollar_volume = _first_float(payload, ("dollar_volume",), ("request", "dollar_volume"))
        if dollar_volume is None and universe is not None:
            dollar_volume = universe.dollar_volume
        spread_score = 100.0 if spread_bps is None else max(0.0, 100.0 - float(spread_bps) * 4.0)
        liquidity_score = 50.0 if dollar_volume is None else max(0.0, min(100.0, float(dollar_volume) / 50_000_000 * 100.0))
        return round((spread_score + liquidity_score) / 2.0, 2)

    def _score_market_regime(self, regime: str | None) -> float:
        if regime in BULLISH_REGIMES:
            return 85.0
        if regime in HOSTILE_REGIMES:
            return 25.0
        if regime == "CHOPPY":
            return 45.0
        return 55.0

    def _score_expectancy(self, expectancy: float | None, sample_size: int) -> float:
        confidence = confidence_from_sample(sample_size)
        if expectancy is None:
            return 40.0 * confidence
        return round(max(0.0, min(100.0, 50.0 + float(expectancy) * 20.0)) * confidence, 2)

    def _penalties(self, payload: dict[str, Any], regime: str | None, expectancy: float | None, sample_size: int) -> list[dict[str, Any]]:
        penalties: list[dict[str, Any]] = []
        spread_bps = _first_float(payload, ("spread_bps",), ("snapshot", "spread_bps"), ("request", "spread_bps"))
        if spread_bps is not None and spread_bps > self.settings.max_spread_bps:
            penalties.append({"code": "WIDE_SPREAD", "points": 20, "reason": "Spread exceeds preferred alpha threshold."})
        if regime in HOSTILE_REGIMES:
            penalties.append({"code": "HOSTILE_REGIME", "points": 15, "reason": "Market regime is hostile to long momentum."})
        if expectancy is not None and expectancy < 0:
            penalties.append({"code": "NEGATIVE_EXPECTANCY", "points": 25, "reason": "Historical cohort has negative expectancy."})
        if sample_size < 20:
            penalties.append({"code": "LOW_SAMPLE_SIZE", "points": 10, "reason": "Historical expectancy confidence is limited."})
        if payload.get("volume_fading"):
            penalties.append({"code": "VOLUME_FADING", "points": 15, "reason": "Volume confirmation is fading."})
        return penalties

    def _explain(self, scanner_result, component_scores: dict[str, float], penalties: list[dict[str, Any]], grade: str) -> str:
        top = sorted(component_scores.items(), key=lambda item: item[1], reverse=True)[:3]
        top_text = ", ".join(f"{name}={score:.1f}" for name, score in top)
        penalty_text = "; ".join(str(p["code"]) for p in penalties) or "none"
        return f"{scanner_result.symbol} {scanner_result.strategy_id or scanner_result.scanner_name} scored {grade}: strongest components {top_text}; penalties {penalty_text}."


def grade_from_score(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    return "WATCH"


def confidence_from_sample(sample_size: int) -> float:
    if sample_size <= 0:
        return 0.25
    return round(min(1.0, sample_size / 50.0), 4)


def suggested_risk_multiplier(*, grade: str, expectancy: float | None, sample_size: int, regime: str | None) -> float:
    if expectancy is not None and expectancy < 0:
        return 0.0
    base = {"A+": 1.0, "A": 0.75, "B": 0.5, "C": 0.1, "WATCH": 0.0}.get(grade, 0.0)
    if expectancy is None or sample_size < 20:
        base *= 0.5
    if regime in HOSTILE_REGIMES:
        base *= 0.5
    return round(base, 4)


def _component_explanation(name: str, score: float) -> str:
    return f"{name.replace('_', ' ').title()} component contributed {score:.1f}/100."


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _first_float(payload: dict[str, Any], *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None:
            try:
                return float(current)
            except (TypeError, ValueError):
                continue
    return None
