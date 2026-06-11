from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository

ALPHA_SCANNER_VERSION = "alpha_event_driven_scanners_v1"
ALPHA_STRATEGIES = {
    "CATALYST_VWAP_RECLAIM",
    "CATALYST_OPENING_RANGE_BREAKOUT",
    "POST_EARNINGS_DRIFT",
    "GAP_AND_HOLD_CONTINUATION",
    "RELATIVE_STRENGTH_PULLBACK_RECLAIM",
    "FAILED_BREAKDOWN_REVERSAL",
    "SECTOR_LEADER_ROTATION",
}
HOSTILE_LONG_REGIMES = {"BEAR_TREND", "RISK_OFF", "HIGH_VOLATILITY", "MACRO_EVENT_RISK"}


@dataclass(frozen=True)
class AlphaScannerRunResult:
    strategy_id: str
    symbols_seen: int
    accepted: int
    rejected: int
    scanner_result_ids: list[str]
    reason: str
    version: str = ALPHA_SCANNER_VERSION


class AlphaStrategyScannerService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.session = repository.session
        self.settings = settings or get_settings()

    def run_strategy(
        self,
        strategy_id: str,
        *,
        symbols: list[str] | None = None,
    ) -> AlphaScannerRunResult:
        normalized = strategy_id.upper()
        if normalized not in ALPHA_STRATEGIES:
            raise ValueError(f"Unknown alpha strategy: {strategy_id}")
        symbol_list = [symbol.upper() for symbol in (symbols or self.repository.active_symbols())]
        accepted = 0
        rejected = 0
        ids: list[str] = []
        for symbol in symbol_list:
            if normalized == "CATALYST_VWAP_RECLAIM":
                decision = self._catalyst_vwap_reclaim(symbol)
            elif normalized == "CATALYST_OPENING_RANGE_BREAKOUT":
                decision = self._catalyst_orb(symbol)
            elif normalized == "POST_EARNINGS_DRIFT":
                decision = self._post_earnings_drift(symbol)
            elif normalized == "GAP_AND_HOLD_CONTINUATION":
                decision = self._gap_and_hold(symbol)
            elif normalized == "RELATIVE_STRENGTH_PULLBACK_RECLAIM":
                decision = self._relative_strength_pullback(symbol)
            elif normalized == "FAILED_BREAKDOWN_REVERSAL":
                decision = self._failed_breakdown_reversal(symbol)
            else:
                decision = self._sector_leader_rotation(symbol)
            ids.append(decision.id)
            accepted += int(decision.accepted)
            rejected += int(not decision.accepted)
        return AlphaScannerRunResult(
            strategy_id=normalized,
            symbols_seen=len(symbol_list),
            accepted=accepted,
            rejected=rejected,
            scanner_result_ids=ids,
            reason=f"Alpha scanner {normalized} evaluated {len(symbol_list)} symbol(s).",
        )

    def run_all(self, *, symbols: list[str] | None = None) -> list[AlphaScannerRunResult]:
        return [
            self.run_strategy(strategy_id, symbols=symbols)
            for strategy_id in sorted(ALPHA_STRATEGIES)
        ]

    def _catalyst_vwap_reclaim(self, symbol: str) -> models.ScannerResult:
        frame = self._intraday(symbol)
        catalyst = self._fresh_catalyst(symbol)
        regime = self._market_regime()
        if frame.empty or len(frame) < 2:
            return self._reject(
                symbol, "CATALYST_VWAP_RECLAIM", "NO_INTRADAY_DATA", "Not enough intraday candles."
            )
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        vwap = _ensure_vwap(frame)
        previous_close = float(previous["close"])
        previous_vwap = float(vwap.iloc[-2])
        latest_close = float(latest["close"])
        latest_vwap = float(vwap.iloc[-1])
        relative_volume = _relative_volume(frame)
        spread_bps = _spread_bps(latest)
        dollar_volume = float((frame["close"] * frame["volume"]).tail(20).mean())
        extended_pct = (
            ((latest_close - latest_vwap) / latest_vwap) * 100 if latest_vwap > 0 else 0.0
        )
        blockers = []
        if catalyst is None:
            blockers.append(("CATALYST_STALE", "Fresh catalyst is required within 24h."))
        if regime in HOSTILE_LONG_REGIMES:
            blockers.append(("HOSTILE_REGIME", f"Market regime {regime} is hostile."))
        if latest_close <= self._previous_daily_close(symbol, latest_close):
            blockers.append(("NO_POSITIVE_REACTION", "Price is not above prior close."))
        if relative_volume < 2.5:
            blockers.append(("LOW_RELATIVE_VOLUME", "Relative volume is below 2.5x."))
        if not (previous_close < previous_vwap and latest_close > latest_vwap):
            blockers.append(("NO_VWAP_RECLAIM", "Price did not reclaim VWAP from below."))
        if spread_bps > self.settings.max_spread_bps:
            blockers.append(("WIDE_SPREAD", "Spread is too wide."))
        if dollar_volume < self.settings.min_dollar_volume:
            blockers.append(("ILLIQUID", "Dollar volume is below configured minimum."))
        if extended_pct > 4.0:
            blockers.append(("EXTENDED_FROM_VWAP", "Price is extended too far above VWAP."))
        payload = {
            "setup_type": "CATALYST_VWAP_RECLAIM",
            "catalyst_id": catalyst.id if catalyst else None,
            "catalyst_type": catalyst.catalyst_type if catalyst else None,
            "relative_volume": relative_volume,
            "spread_bps": spread_bps,
            "dollar_volume": dollar_volume,
            "latest_close": latest_close,
            "latest_vwap": latest_vwap,
            "previous_close": previous_close,
            "previous_vwap": previous_vwap,
            "vwap_reclaim": previous_close < previous_vwap and latest_close > latest_vwap,
            "price_above_previous_close": latest_close
            > self._previous_daily_close(symbol, latest_close),
            "volume_expansion": float(latest["volume"]) > float(frame["volume"].tail(10).mean()),
            "stop_candidates": {
                "below_vwap": round(latest_vwap * 0.995, 4),
                "reclaim_candle_low": float(latest["low"]),
            },
            "targets": {
                "one_r": None,
                "two_r": None,
                "previous_intraday_high": float(frame["high"].max()),
            },
            "market_regime": regime,
        }
        return self._persist(symbol, "CATALYST_VWAP_RECLAIM", blockers, payload)

    def _catalyst_orb(self, symbol: str) -> models.ScannerResult:
        frame = self._intraday(symbol)
        catalyst = self._fresh_catalyst(symbol)
        regime = self._market_regime()
        if frame.empty or len(frame) < 16:
            return self._reject(
                symbol,
                "CATALYST_OPENING_RANGE_BREAKOUT",
                "NO_OPENING_RANGE",
                "At least 16 intraday candles are required.",
            )
        vwap = _ensure_vwap(frame)
        opening = frame.iloc[:15]
        latest = frame.iloc[-1]
        opening_high = float(opening["high"].max())
        opening_low = float(opening["low"].min())
        opening_midpoint = (opening_high + opening_low) / 2.0
        latest_close = float(latest["close"])
        relative_volume = _relative_volume(frame)
        gap_pct = self._gap_pct(symbol, latest_close)
        spread_bps = _spread_bps(latest)
        blockers = []
        if catalyst is None:
            blockers.append(("CATALYST_STALE", "Fresh catalyst is required within 24h."))
        if latest_close <= opening_high:
            blockers.append(("NO_ORB_BREAK", "Latest close is not above opening-range high."))
        if relative_volume < 2.0:
            blockers.append(("LOW_RELATIVE_VOLUME", "Relative volume is below ORB threshold."))
        if latest_close <= float(vwap.iloc[-1]):
            blockers.append(("BELOW_VWAP", "Breakout close is not above VWAP."))
        if gap_pct <= 0:
            blockers.append(("GAP_NOT_ALIGNED", "Gap direction is not aligned with long breakout."))
        if regime in HOSTILE_LONG_REGIMES:
            blockers.append(("HOSTILE_REGIME", f"Market regime {regime} is hostile."))
        if spread_bps > self.settings.max_spread_bps:
            blockers.append(("WIDE_SPREAD", "Spread/slippage is not acceptable."))
        payload = {
            "setup_type": "CATALYST_OPENING_RANGE_BREAKOUT",
            "catalyst_id": catalyst.id if catalyst else None,
            "catalyst_type": catalyst.catalyst_type if catalyst else None,
            "opening_range_high": opening_high,
            "opening_range_low": opening_low,
            "opening_range_midpoint": opening_midpoint,
            "opening_range_breakout": latest_close > opening_high,
            "relative_volume": relative_volume,
            "gap_pct": gap_pct,
            "spread_bps": spread_bps,
            "latest_close": latest_close,
            "latest_vwap": float(vwap.iloc[-1]),
            "stops": {"opening_range_midpoint": opening_midpoint, "opening_range_low": opening_low},
            "targets": {
                "measured_move": latest_close + (opening_high - opening_low),
                "one_r": None,
                "two_r": None,
            },
            "market_regime": regime,
        }
        return self._persist(symbol, "CATALYST_OPENING_RANGE_BREAKOUT", blockers, payload)

    def _post_earnings_drift(self, symbol: str) -> models.ScannerResult:
        frame = self._daily(symbol)
        catalyst = self._fresh_catalyst(
            symbol, catalyst_types={"earnings_or_fundamental_filing", "earnings"}, hours=72
        )
        if frame.empty or len(frame) < 2:
            return self._reject(
                symbol,
                "POST_EARNINGS_DRIFT",
                "NO_DAILY_DATA",
                "At least two daily candles are required.",
            )
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        gap_pct = (
            ((float(latest["open"]) - float(previous["close"])) / float(previous["close"])) * 100
            if float(previous["close"]) > 0
            else 0.0
        )
        close_location = _close_location(latest)
        rv = _relative_volume(frame)
        rs = self._relative_strength(symbol)
        blockers = []
        if catalyst is None:
            blockers.append(("NO_EARNINGS_CATALYST", "Confirmed earnings catalyst is required."))
        if gap_pct <= 0:
            blockers.append(("NO_EARNINGS_GAP_UP", "Earnings drift long requires a gap up."))
        if rv < 1.5:
            blockers.append(("LOW_RELATIVE_VOLUME", "Post-earnings volume is not elevated."))
        if close_location < 0.65:
            blockers.append(("WEAK_CLOSE", "Close is not near high of day."))
        if rs <= 0:
            blockers.append(
                ("WEAK_RELATIVE_STRENGTH", "Relative strength vs SPY/sector is not positive.")
            )
        payload = {
            "setup_type": "POST_EARNINGS_DRIFT",
            "catalyst_id": catalyst.id if catalyst else None,
            "catalyst_type": catalyst.catalyst_type if catalyst else None,
            "gap_pct": gap_pct,
            "relative_volume": rv,
            "close_location": close_location,
            "relative_strength_20d": rs,
            "hold_days": {"min": 1, "max": 5},
            "stops": {"prior_day_low": float(previous["low"])},
            "targets": {"two_r": None, "three_r": None, "time_exit_days": 5},
        }
        return self._persist(symbol, "POST_EARNINGS_DRIFT", blockers, payload)

    def _gap_and_hold(self, symbol: str) -> models.ScannerResult:
        frame = self._intraday(symbol)
        if frame.empty or len(frame) < 30:
            return self._reject(
                symbol,
                "GAP_AND_HOLD_CONTINUATION",
                "NO_INTRADAY_DATA",
                "Gap-and-hold requires 30+ intraday candles.",
            )
        latest = frame.iloc[-1]
        vwap = _ensure_vwap(frame)
        gap_pct = self._gap_pct(symbol, float(latest["close"]))
        rv = _relative_volume(frame)
        first_30_low = float(frame.iloc[:30]["low"].min())
        prior_close = self._previous_daily_close(symbol, float(latest["close"]))
        blockers = []
        if not (3.0 <= gap_pct <= 20.0):
            blockers.append(("GAP_OUT_OF_RANGE", "Gap must be between 3% and 20%."))
        if rv < 2.0:
            blockers.append(("LOW_RELATIVE_VOLUME", "Abnormal volume is required."))
        if first_30_low <= prior_close:
            blockers.append(("GAP_FILLED", "Gap filled during the first 30 minutes."))
        if float(latest["close"]) <= float(vwap.iloc[-1]):
            blockers.append(("LOST_VWAP", "Price lost VWAP."))
        payload = {
            "setup_type": "GAP_AND_HOLD_CONTINUATION",
            "gap_pct": gap_pct,
            "relative_volume": rv,
            "latest_close": float(latest["close"]),
            "latest_vwap": float(vwap.iloc[-1]),
            "consolidation_high": float(frame["high"].tail(10).max()),
        }
        return self._persist(symbol, "GAP_AND_HOLD_CONTINUATION", blockers, payload)

    def _relative_strength_pullback(self, symbol: str) -> models.ScannerResult:
        frame = self._intraday(symbol)
        rs = self._relative_strength(symbol)
        regime = self._market_regime()
        if frame.empty or len(frame) < 20:
            return self._reject(
                symbol,
                "RELATIVE_STRENGTH_PULLBACK_RECLAIM",
                "NO_INTRADAY_DATA",
                "Pullback strategy requires 20+ intraday candles.",
            )
        latest = frame.iloc[-1]
        vwap = _ensure_vwap(frame)
        ema20 = frame["close"].ewm(span=20, adjust=False).mean()
        reclaim = float(frame["close"].iloc[-2]) < float(vwap.iloc[-2]) and float(
            latest["close"]
        ) > float(vwap.iloc[-1])
        blockers = []
        if rs <= 2.0:
            blockers.append(
                ("WEAK_RELATIVE_STRENGTH", "Stock is not outperforming SPY/sector enough.")
            )
        if regime in HOSTILE_LONG_REGIMES:
            blockers.append(("HOSTILE_REGIME", "Regime is not bullish or neutral."))
        if float(latest["close"]) < float(ema20.iloc[-1]) and not reclaim:
            blockers.append(("NO_RECLAIM", "Pullback did not reclaim VWAP/EMA support."))
        if _relative_volume(frame) < 1.5:
            blockers.append(("NO_VOLUME_CONFIRMATION", "Reclaim lacks volume."))
        payload = {
            "setup_type": "RELATIVE_STRENGTH_PULLBACK_RECLAIM",
            "relative_strength_20d": rs,
            "relative_volume": _relative_volume(frame),
            "latest_close": float(latest["close"]),
            "latest_vwap": float(vwap.iloc[-1]),
            "ema20": float(ema20.iloc[-1]),
            "vwap_reclaim": reclaim,
            "market_regime": regime,
        }
        return self._persist(symbol, "RELATIVE_STRENGTH_PULLBACK_RECLAIM", blockers, payload)

    def _failed_breakdown_reversal(self, symbol: str) -> models.ScannerResult:
        frame = self._intraday(symbol)
        daily = self._daily(symbol)
        if frame.empty or daily.empty or len(frame) < 5 or len(daily) < 2:
            return self._reject(
                symbol,
                "FAILED_BREAKDOWN_REVERSAL",
                "NO_CONTEXT",
                "Requires intraday and prior daily support context.",
            )
        latest = frame.iloc[-1]
        prior_low = float(daily.iloc[-2]["low"])
        intraday_low = float(frame["low"].min())
        vwap = _ensure_vwap(frame)
        reclaimed_support = intraday_low < prior_low and float(latest["close"]) > prior_low
        reclaimed_vwap = float(latest["close"]) > float(vwap.iloc[-1])
        short_interest = self.repository.latest_short_interest_for(symbol)
        blockers = []
        if not reclaimed_support:
            blockers.append(
                ("NO_FAILED_BREAKDOWN", "Price did not break then reclaim key support.")
            )
        if not reclaimed_vwap:
            blockers.append(("NO_VWAP_RECLAIM", "Reversal has not reclaimed VWAP."))
        if _relative_volume(frame) < 1.8:
            blockers.append(("LOW_ATTENTION", "Relative volume is not high enough."))
        if short_interest is None:
            blockers.append(
                (
                    "NO_SHORT_INTEREST_CONTEXT",
                    "Failed-breakdown squeeze setup requires short-interest context.",
                )
            )
        elif short_interest.short_score < 35:
            blockers.append(
                (
                    "LOW_SQUEEZE_PRESSURE",
                    "Short interest, days-to-cover, borrow/utilization, or float do not confirm squeeze pressure.",
                )
            )
        payload = {
            "setup_type": "FAILED_BREAKDOWN_REVERSAL",
            "prior_day_low": prior_low,
            "failed_breakdown_low": intraday_low,
            "latest_close": float(latest["close"]),
            "latest_vwap": float(vwap.iloc[-1]),
            "relative_volume": _relative_volume(frame),
            "short_interest_pct_float": short_interest.short_interest_pct_float
            if short_interest
            else None,
            "days_to_cover": short_interest.days_to_cover if short_interest else None,
            "borrow_fee_pct": short_interest.borrow_fee_pct if short_interest else None,
            "utilization_pct": short_interest.utilization_pct if short_interest else None,
            "float_shares": short_interest.float_shares if short_interest else None,
            "short_score": short_interest.short_score if short_interest else None,
            "targets": {
                "vwap": float(vwap.iloc[-1]),
                "prior_day_high": float(daily.iloc[-2]["high"]),
            },
        }
        return self._persist(symbol, "FAILED_BREAKDOWN_REVERSAL", blockers, payload)

    def _sector_leader_rotation(self, symbol: str) -> models.ScannerResult:
        relative = self.session.scalar(
            select(models.SymbolRelativeStrengthSnapshot)
            .where(models.SymbolRelativeStrengthSnapshot.symbol == symbol)
            .order_by(
                desc(models.SymbolRelativeStrengthSnapshot.source_timestamp),
                desc(models.SymbolRelativeStrengthSnapshot.created_at),
            )
            .limit(1)
        )
        blockers = []
        if relative is None:
            blockers.append(
                (
                    "NO_LEADERSHIP_SNAPSHOT",
                    "Sector leadership refresh has not produced a symbol snapshot.",
                )
            )
        elif (relative.stock_vs_spy_score or 0.0) < 55 or (
            relative.stock_vs_sector_score or 0.0
        ) < 55:
            blockers.append(("NOT_A_LEADER", "Stock and sector relative strength are not aligned."))
        payload = {
            "setup_type": "SECTOR_LEADER_ROTATION",
            "stock_vs_spy_score": relative.stock_vs_spy_score if relative else None,
            "stock_vs_sector_score": relative.stock_vs_sector_score if relative else None,
            "leadership_rank": relative.leadership_rank if relative else None,
        }
        return self._persist(symbol, "SECTOR_LEADER_ROTATION", blockers, payload)

    def _persist(
        self,
        symbol: str,
        strategy_id: str,
        blockers: list[tuple[str, str]],
        payload: dict[str, Any],
    ) -> models.ScannerResult:
        accepted = not blockers
        score = 75.0 if accepted else 0.0
        reason = (
            f"{strategy_id} accepted with event-driven alpha confirmation."
            if accepted
            else "; ".join(reason for _code, reason in blockers)
        )
        row = self.repository.store_generic_scanner_result(
            scanner_name=strategy_id,
            scanner_rule_version=ALPHA_SCANNER_VERSION,
            symbol=symbol,
            strategy_id=strategy_id,
            accepted=accepted,
            score=score,
            reason=reason,
            payload={**payload, "rejection_codes": [code for code, _reason in blockers]},
            source_timestamp=datetime.now(UTC),
        )
        for code, blocker_reason in blockers:
            self.repository.store_alpha_rejection_reason(
                scanner_result_id=row.id,
                symbol=symbol,
                strategy_id=strategy_id,
                setup_type=payload.get("setup_type"),
                reason_code=code,
                reason=blocker_reason,
                payload=payload,
                source_timestamp=row.source_timestamp,
            )
        return row

    def _reject(
        self, symbol: str, strategy_id: str, code: str, reason: str
    ) -> models.ScannerResult:
        return self._persist(symbol, strategy_id, [(code, reason)], {"setup_type": strategy_id})

    def _intraday(self, symbol: str) -> pd.DataFrame:
        return self.repository.clean_candles_df(
            symbol, timeframe="1Min", provider="alpaca_market_data", limit=390, valid_only=True
        )

    def _daily(self, symbol: str) -> pd.DataFrame:
        frame = self.repository.clean_candles_df(
            symbol, timeframe="1D", provider="alpaca_market_data", limit=80, valid_only=True
        )
        if frame.empty:
            frame = self.repository.clean_candles_df(
                symbol, timeframe="1D", provider="yahoo_chart", limit=80, valid_only=True
            )
        return frame

    def _fresh_catalyst(self, symbol: str, catalyst_types: set[str] | None = None, hours: int = 24):
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        query = select(models.Catalyst).where(
            models.Catalyst.symbol == symbol, models.Catalyst.source_timestamp >= cutoff
        )
        if catalyst_types:
            query = query.where(models.Catalyst.catalyst_type.in_(catalyst_types))
        return self.session.scalar(
            query.order_by(
                desc(models.Catalyst.source_timestamp), desc(models.Catalyst.created_at)
            ).limit(1)
        )

    def _market_regime(self) -> str | None:
        row = self.session.scalar(
            select(models.MarketRegimeSnapshot)
            .order_by(
                desc(models.MarketRegimeSnapshot.source_timestamp),
                desc(models.MarketRegimeSnapshot.created_at),
            )
            .limit(1)
        )
        return row.market_regime if row else None

    def _previous_daily_close(self, symbol: str, fallback: float) -> float:
        daily = self._daily(symbol)
        if len(daily) >= 2:
            return float(daily.iloc[-2]["close"])
        return fallback

    def _gap_pct(self, symbol: str, current: float) -> float:
        previous = self._previous_daily_close(symbol, current)
        return ((current - previous) / previous) * 100 if previous > 0 else 0.0

    def _relative_strength(self, symbol: str) -> float:
        feature = self.session.scalar(
            select(models.SymbolFeatureSnapshot)
            .where(models.SymbolFeatureSnapshot.symbol == symbol)
            .order_by(
                desc(models.SymbolFeatureSnapshot.source_timestamp),
                desc(models.SymbolFeatureSnapshot.created_at),
            )
            .limit(1)
        )
        if feature and isinstance(feature.snapshot, dict):
            return float(feature.snapshot.get("relative_strength_20d") or 0.0)
        return 0.0


def _ensure_vwap(frame: pd.DataFrame) -> pd.Series:
    if "vwap" in frame and not frame["vwap"].isna().all():
        return frame["vwap"].ffill()
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    return (typical * frame["volume"]).cumsum() / frame["volume"].replace(0, pd.NA).cumsum()


def _relative_volume(frame: pd.DataFrame) -> float:
    lookback = frame["volume"].tail(min(30, len(frame)))
    average = max(1.0, float(lookback.mean()))
    return round(float(frame["volume"].iloc[-1]) / average, 4)


def _spread_bps(row: pd.Series) -> float:
    low = float(row["low"])
    high = float(row["high"])
    mid = (high + low) / 2.0
    return ((high - low) / mid) * 10_000 if mid > 0 else 0.0


def _close_location(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    if high <= low:
        return 0.5
    return (float(row["close"]) - low) / (high - low)
