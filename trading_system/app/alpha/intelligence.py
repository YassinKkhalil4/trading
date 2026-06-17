from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from trading_system.app.alpha.leadership import SECTOR_ETFS
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository


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


@dataclass(frozen=True)
class IntelligenceRefreshResult:
    records_created: int
    reason: str


class PointInTimeUniverseService:
    """Creates auditable point-in-time universe membership snapshots for backtests."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def snapshot_current_universe(
        self,
        *,
        universe_name: str = "tradable_us_equities",
        as_of: datetime | None = None,
    ) -> IntelligenceRefreshResult:
        timestamp = _as_utc(as_of)
        rows = self.session.scalars(
            select(models.SymbolUniverse).order_by(models.SymbolUniverse.symbol.asc())
        ).all()
        created = 0
        for row in rows:
            self.repository.store_point_in_time_universe_membership(
                universe_name=universe_name,
                as_of_date=timestamp,
                symbol=row.symbol,
                name=row.name,
                asset_class=row.asset_class,
                exchange=row.exchange,
                sector=row.sector,
                industry=row.industry,
                is_active=row.is_active,
                is_tradable=row.is_tradable,
                is_liquid=row.is_liquid,
                effective_from=row.source_timestamp,
                effective_to=None if row.is_active else row.updated_at,
                delisted=not row.is_active,
                membership_reason=row.tradability_reason
                or row.change_reason
                or "Copied from current symbol universe snapshot.",
                provider=row.provider_status,
                payload={
                    "provider_asset_id": row.provider_asset_id,
                    "disable_reason": row.disable_reason,
                    "liquidity_rank": row.liquidity_rank,
                    "dollar_volume": row.dollar_volume,
                    "spread_bps": row.spread_bps,
                    "survivorship_bias_control": True,
                },
                source_timestamp=timestamp,
            )
            created += 1
        return IntelligenceRefreshResult(
            created,
            f"Point-in-time universe {universe_name} snapshotted for {timestamp.date()}.",
        )


class ShortInterestService:
    """Persists short-interest context used by squeeze/reversal logic."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def refresh_from_universe_payloads(
        self, symbols: list[str] | None = None
    ) -> IntelligenceRefreshResult:
        query = select(models.SymbolUniverse)
        if symbols:
            query = query.where(
                models.SymbolUniverse.symbol.in_([symbol.upper() for symbol in symbols])
            )
        rows = self.session.scalars(query.order_by(models.SymbolUniverse.symbol.asc())).all()
        created = 0
        for row in rows:
            payload = row.raw_asset_payload if isinstance(row.raw_asset_payload, dict) else {}
            short_payload = _nested(payload, "short_interest") or payload
            snapshot = self.store_snapshot(
                row.symbol,
                short_interest_pct_float=_first_float(
                    short_payload,
                    "short_interest_pct_float",
                    "shortPercentOfFloat",
                    "short_float_pct",
                ),
                days_to_cover=_first_float(short_payload, "days_to_cover", "shortRatio"),
                borrow_fee_pct=_first_float(
                    short_payload, "borrow_fee_pct", "borrowFee", "borrow_fee"
                ),
                utilization_pct=_first_float(short_payload, "utilization_pct", "utilization"),
                float_shares=_first_float(short_payload, "float_shares", "floatShares", "float"),
                provider=str(
                    payload.get("short_interest_provider")
                    or payload.get("provider")
                    or "universe_payload"
                ),
            )
            if snapshot.data_confidence > 0:
                created += 1
        return IntelligenceRefreshResult(
            created, "Short-interest layer refreshed from available provider/universe payloads."
        )

    def store_snapshot(self, symbol: str, **kwargs: Any) -> models.ShortInterestSnapshot:
        values = {
            "short_interest_pct_float": kwargs.get("short_interest_pct_float"),
            "days_to_cover": kwargs.get("days_to_cover"),
            "borrow_fee_pct": kwargs.get("borrow_fee_pct"),
            "utilization_pct": kwargs.get("utilization_pct"),
            "float_shares": kwargs.get("float_shares"),
        }
        score = _short_score(**values)
        confidence = _confidence(values.values())
        reason = _short_reason(score, confidence)
        return self.repository.store_short_interest_snapshot(
            symbol=symbol,
            **values,
            short_score=score,
            data_confidence=confidence,
            provider=kwargs.get("provider"),
            reason=reason,
            source_timestamp=kwargs.get("source_timestamp") or datetime.now(UTC),
        )


class OptionsIntelligenceService:
    """Persists options context for weekly-options and earnings-options candidates."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def refresh_from_universe_payloads(
        self, symbols: list[str] | None = None
    ) -> IntelligenceRefreshResult:
        query = select(models.SymbolUniverse)
        if symbols:
            query = query.where(
                models.SymbolUniverse.symbol.in_([symbol.upper() for symbol in symbols])
            )
        rows = self.session.scalars(query.order_by(models.SymbolUniverse.symbol.asc())).all()
        created = 0
        for row in rows:
            payload = row.raw_asset_payload if isinstance(row.raw_asset_payload, dict) else {}
            options_payload = _nested(payload, "options") or payload
            snapshot = self.store_snapshot(
                row.symbol,
                iv_rank=_first_float(options_payload, "iv_rank", "ivRank"),
                iv_percentile=_first_float(options_payload, "iv_percentile", "ivPercentile"),
                open_interest=_first_float(options_payload, "open_interest", "openInterest"),
                gamma_exposure=_first_float(options_payload, "gamma_exposure", "gamma", "gex"),
                delta_exposure=_first_float(options_payload, "delta_exposure", "delta"),
                expected_move_pct=_first_float(
                    options_payload, "expected_move_pct", "expectedMovePct"
                ),
                weekly_expiry=bool(
                    options_payload.get("weekly_expiry") or options_payload.get("weeklyExpiry")
                ),
                earnings_expiry=bool(
                    options_payload.get("earnings_expiry") or options_payload.get("earningsExpiry")
                ),
                provider=str(
                    payload.get("options_provider") or payload.get("provider") or "universe_payload"
                ),
            )
            if snapshot.data_confidence > 0:
                created += 1
        return IntelligenceRefreshResult(
            created,
            "Options-intelligence layer refreshed from available provider/universe payloads.",
        )

    def store_snapshot(self, symbol: str, **kwargs: Any) -> models.OptionsIntelligenceSnapshot:
        values = {
            "iv_rank": kwargs.get("iv_rank"),
            "iv_percentile": kwargs.get("iv_percentile"),
            "open_interest": kwargs.get("open_interest"),
            "gamma_exposure": kwargs.get("gamma_exposure"),
            "delta_exposure": kwargs.get("delta_exposure"),
            "expected_move_pct": kwargs.get("expected_move_pct"),
        }
        score = _options_score(**values)
        confidence = _confidence(values.values())
        return self.repository.store_options_intelligence_snapshot(
            symbol=symbol,
            **values,
            options_score=score,
            weekly_expiry=bool(kwargs.get("weekly_expiry")),
            earnings_expiry=bool(kwargs.get("earnings_expiry")),
            data_confidence=confidence,
            provider=kwargs.get("provider"),
            reason=f"Options score {score:.1f} with {confidence:.0%} field coverage.",
            source_timestamp=kwargs.get("source_timestamp") or datetime.now(UTC),
        )


class MultiBaggerScoringService:
    """Separate long-shot/narrative scoring from intraday alpha scoring."""

    COMPONENT_WEIGHTS = {
        "narrative": 25.0,
        "growth": 20.0,
        "capital_flows": 20.0,
        "institutional_accumulation": 15.0,
        "short_squeeze": 10.0,
        "options_leverage": 10.0,
    }

    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def score_universe(self, symbols: list[str] | None = None) -> IntelligenceRefreshResult:
        query = select(models.SymbolUniverse).where(models.SymbolUniverse.is_active.is_(True))
        if symbols:
            query = query.where(
                models.SymbolUniverse.symbol.in_([symbol.upper() for symbol in symbols])
            )
        rows = self.session.scalars(query.order_by(models.SymbolUniverse.symbol.asc())).all()
        created = 0
        for row in rows:
            self.score_symbol(row.symbol)
            created += 1
        return IntelligenceRefreshResult(
            created, "Multi-bagger long-shot candidates scored separately from intraday alpha."
        )

    def score_symbol(self, symbol: str) -> models.MultiBaggerCandidateScore:
        symbol = symbol.upper()
        universe = self.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol)
        )
        feature = self._latest_feature(symbol)
        news = self._latest_news(symbol)
        short = self.repository.latest_short_interest_for(symbol)
        options = self.repository.latest_options_intelligence_for(symbol)
        payload = (
            universe.raw_asset_payload
            if universe and isinstance(universe.raw_asset_payload, dict)
            else {}
        )
        component_scores = {
            "narrative": _bounded(
                (news.relevance_score or 0.0) * 70.0 + (news.source_confidence_score or 0.0) * 0.3
                if news
                else 20.0
            ),
            "growth": _bounded(
                _first_float(payload, "revenue_growth", "growth_score")
                or (
                    feature.snapshot.get("growth_score", 0.0)
                    if feature and isinstance(feature.snapshot, dict)
                    else 0.0
                )
            ),
            "capital_flows": _bounded(
                50.0 + (_first_float(payload, "fund_flows_score", "capital_flows_score") or 0.0)
            ),
            "institutional_accumulation": _bounded(
                _first_float(payload, "institutional_accumulation_score", "accumulation_score")
                or (
                    60.0
                    if universe and universe.dollar_volume and universe.dollar_volume > 50_000_000
                    else 35.0
                )
            ),
            "short_squeeze": float(short.short_score if short else 0.0),
            "options_leverage": float(options.options_score if options else 0.0),
        }
        weighted = sum(
            component_scores[name] * weight for name, weight in self.COMPONENT_WEIGHTS.items()
        )
        score = round(_bounded(weighted / sum(self.COMPONENT_WEIGHTS.values())), 2)
        risk_flags = _multi_bagger_risk_flags(universe, short, options, component_scores)
        for flag in risk_flags:
            score = max(0.0, score - float(flag.get("points", 0.0)))
        grade = grade_from_score(score)
        target_multiple = (
            "10x" if score >= 90 else "5x" if score >= 75 else "3x" if score >= 60 else "watch"
        )
        confidence = _confidence(
            [
                news.relevance_score if news else None,
                component_scores["growth"] if component_scores["growth"] > 0 else None,
                component_scores["capital_flows"],
                short.short_score if short else None,
                options.options_score if options else None,
            ]
        )
        narrative = (
            f"{symbol} long-shot score {score:.1f}: narrative={component_scores['narrative']:.1f}, "
            f"growth={component_scores['growth']:.1f}, flows={component_scores['capital_flows']:.1f}."
        )
        return self.repository.store_multi_bagger_candidate_score(
            symbol=symbol,
            horizon="multi_bagger_long_shot",
            score=score,
            grade=grade,
            target_multiple=target_multiple,
            component_scores=component_scores,
            narrative=narrative,
            growth_score=component_scores["growth"],
            capital_flows_score=component_scores["capital_flows"],
            institutional_accumulation_score=component_scores["institutional_accumulation"],
            short_squeeze_score=component_scores["short_squeeze"],
            options_leverage_score=component_scores["options_leverage"],
            risk_flags=risk_flags,
            confidence_level=confidence,
            payload={"sector_etf": SECTOR_ETFS.get(universe.sector or "") if universe else None},
            source_timestamp=datetime.now(UTC),
        )

    def _latest_feature(self, symbol: str) -> models.SymbolFeatureSnapshot | None:
        return self.session.scalar(
            select(models.SymbolFeatureSnapshot)
            .where(models.SymbolFeatureSnapshot.symbol == symbol)
            .order_by(
                desc(models.SymbolFeatureSnapshot.source_timestamp),
                desc(models.SymbolFeatureSnapshot.created_at),
            )
            .limit(1)
        )

    def _latest_news(self, symbol: str) -> models.CleanNews | None:
        return self.session.scalar(
            select(models.CleanNews)
            .where(models.CleanNews.symbol == symbol)
            .order_by(desc(models.CleanNews.source_timestamp), desc(models.CleanNews.created_at))
            .limit(1)
        )


def _as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _nested(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = payload.get(key)
    return value if isinstance(value, dict) else None


def _first_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _bounded(value: float | None) -> float:
    return round(max(0.0, min(100.0, float(value or 0.0))), 2)


def _confidence(values: Any) -> float:
    values = list(values)
    if not values:
        return 0.0
    return round(sum(1 for value in values if value is not None) / len(values), 4)


def _short_score(
    *,
    short_interest_pct_float: float | None,
    days_to_cover: float | None,
    borrow_fee_pct: float | None,
    utilization_pct: float | None,
    float_shares: float | None,
) -> float:
    score = 0.0
    if short_interest_pct_float is not None:
        score += min(35.0, short_interest_pct_float * 1.4)
    if days_to_cover is not None:
        score += min(20.0, days_to_cover * 4.0)
    if borrow_fee_pct is not None:
        score += min(20.0, borrow_fee_pct * 0.8)
    if utilization_pct is not None:
        score += min(20.0, utilization_pct * 0.2)
    if float_shares is not None and float_shares > 0:
        score += 5.0 if float_shares <= 50_000_000 else 2.0 if float_shares <= 150_000_000 else 0.0
    return _bounded(score)


def _short_reason(score: float, confidence: float) -> str:
    if confidence == 0:
        return "No short-interest provider fields available; squeeze context is unknown."
    if score >= 70:
        return "High squeeze pressure from short interest, borrow/utilization, days-to-cover, or float constraints."
    if score >= 40:
        return "Moderate squeeze pressure; use as confirmation rather than primary thesis."
    return "Low squeeze pressure based on available short-interest fields."


def _options_score(
    *,
    iv_rank: float | None,
    iv_percentile: float | None,
    open_interest: float | None,
    gamma_exposure: float | None,
    delta_exposure: float | None,
    expected_move_pct: float | None,
) -> float:
    score = 0.0
    if iv_rank is not None:
        score += min(20.0, iv_rank * 0.2)
    if iv_percentile is not None:
        score += min(20.0, iv_percentile * 0.2)
    if open_interest is not None:
        score += min(20.0, open_interest / 50_000 * 20.0)
    if gamma_exposure is not None:
        score += min(15.0, abs(gamma_exposure) / 1_000_000 * 15.0)
    if delta_exposure is not None:
        score += min(10.0, abs(delta_exposure) / 1_000_000 * 10.0)
    if expected_move_pct is not None:
        score += min(15.0, expected_move_pct * 1.5)
    return _bounded(score)


def _multi_bagger_risk_flags(
    universe: models.SymbolUniverse | None,
    short: models.ShortInterestSnapshot | None,
    options: models.OptionsIntelligenceSnapshot | None,
    components: dict[str, float],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if universe and universe.dollar_volume is not None and universe.dollar_volume < 2_000_000:
        flags.append(
            {
                "code": "THIN_LIQUIDITY",
                "points": 15,
                "reason": "Dollar volume is too low for reliable accumulation evidence.",
            }
        )
    if short and short.data_confidence < 0.4:
        flags.append(
            {
                "code": "LOW_SHORT_DATA_CONFIDENCE",
                "points": 5,
                "reason": "Short-interest data is incomplete.",
            }
        )
    if options and options.data_confidence < 0.4:
        flags.append(
            {
                "code": "LOW_OPTIONS_DATA_CONFIDENCE",
                "points": 5,
                "reason": "Options data is incomplete.",
            }
        )
    if components["narrative"] < 30 and components["growth"] < 30:
        flags.append(
            {
                "code": "WEAK_NARRATIVE_GROWTH",
                "points": 20,
                "reason": "No strong narrative or growth evidence.",
            }
        )
    return flags
