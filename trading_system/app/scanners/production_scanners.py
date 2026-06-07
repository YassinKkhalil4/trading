from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository, model_to_dict


PRODUCTION_SCANNER_VERSION = "production_scanners_v2"
PRODUCTION_DATA_PROVIDER = "alpaca_market_data"
CATALYST_MAX_AGE_DAYS = 7
DAILY_DATA_FRESHNESS_SECONDS = 36 * 60 * 60
SCANNER_TRIGGER_COOLDOWN_MINUTES = 30
REQUIRED_STRATEGY_IDS = (
    "VWAP_RECLAIM",
    "POST_EARNINGS_CONTINUATION",
    "OPENING_RANGE_BREAKOUT",
    "NEWS_MOMENTUM",
    "CATALYST_RUN_UP",
    "RELATIVE_STRENGTH",
    "SECTOR_LEADERSHIP",
)
SCANNER_APPROVED_STATUSES = {
    StrategyStatus.PAPER_TESTING.value,
    StrategyStatus.APPROVED_SMALL_SIZE.value,
    StrategyStatus.APPROVED_FULL_SIZE.value,
}
CATALYST_REQUIRED_STRATEGY_IDS = {
    "NEWS_MOMENTUM",
    "CATALYST_RUN_UP",
    "POST_EARNINGS_CONTINUATION",
}


@dataclass(frozen=True)
class ProductionScannerRunResult:
    symbols_seen: int
    scanners_run: int
    accepted: int
    rejected: int
    reason: str
    version: str = PRODUCTION_SCANNER_VERSION


@dataclass(frozen=True)
class ScannerPreflight:
    allowed: bool
    reason: str
    payload: dict[str, Any]
    frame: Any
    provider: str | None


class ProductionStrategyScanner:
    strategy_id: str
    timeframe: str
    catalyst_types: set[str] | None = None
    requires_catalyst: bool = False

    def __init__(self, engine: "ProductionScannerEngine") -> None:
        self.engine = engine

    def scan(self, symbol: str) -> dict:
        preflight = self.engine._preflight(symbol, self.strategy_id, self.timeframe)
        if not preflight.allowed:
            return self.engine._blocked_decision(self.strategy_id, symbol, preflight)

        catalyst = self.engine._latest_catalyst(symbol, self.catalyst_types)
        if self.engine._strategy_requires_catalyst(self.strategy_id, self.requires_catalyst) and not catalyst:
            return self.engine._decision(
                self.strategy_id,
                self.strategy_id,
                symbol,
                False,
                0.0,
                "Required catalyst is missing for strategy preflight.",
                {"preflight": preflight.payload, "catalyst_id": None},
            )

        return self._evaluate(symbol, preflight, catalyst)

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        raise NotImplementedError


class VwapReclaimProductionScanner(ProductionStrategyScanner):
    strategy_id = "VWAP_RECLAIM"
    timeframe = "1Min"

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        frame = preflight.frame
        if len(frame) < 2 or "vwap" not in frame or frame["vwap"].tail(2).isna().any():
            return self.engine._decision(
                self.strategy_id,
                self.strategy_id,
                symbol,
                False,
                0.0,
                "Not enough intraday VWAP candles for reclaim scan.",
                {"preflight": preflight.payload, "rows": len(frame), "catalyst_id": catalyst.id if catalyst else None},
            )
        previous_close = float(frame["close"].iloc[-2])
        previous_vwap = float(frame["vwap"].iloc[-2])
        latest_close = float(frame["close"].iloc[-1])
        latest_vwap = float(frame["vwap"].iloc[-1])
        average_volume = max(1.0, float(frame["volume"].tail(min(30, len(frame))).mean()))
        relative_volume = float(frame["volume"].iloc[-1]) / average_volume
        feature = self.engine._latest_feature_snapshot(symbol)
        relative_strength = (
            feature.snapshot.get("relative_strength_20d")
            if feature and isinstance(feature.snapshot, dict)
            else None
        )
        reclaim = previous_close < previous_vwap and latest_close > latest_vwap
        volume_ok = relative_volume > 1.5
        confirmation = relative_strength is not None and float(relative_strength) > 2.0
        accepted = reclaim and volume_ok and confirmation
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            min(100.0, 62.0 + min(relative_volume, 4.0) * 6.0 + 8.0) if accepted else 0.0,
            "VWAP reclaim from below confirmed with volume and catalyst/relative-strength support."
            if accepted
            else "VWAP reclaim conditions not met.",
            {
                "preflight": preflight.payload,
                "previous_close": previous_close,
                "previous_vwap": previous_vwap,
                "latest_close": latest_close,
                "latest_vwap": latest_vwap,
                "relative_volume": relative_volume,
                "catalyst_id": catalyst.id if catalyst else None,
                "relative_strength_20d": relative_strength,
            },
        )


class PostEarningsContinuationScanner(ProductionStrategyScanner):
    strategy_id = "POST_EARNINGS_CONTINUATION"
    timeframe = "1D"
    catalyst_types = {"earnings_or_fundamental_filing", "material_filing"}
    requires_catalyst = True

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        frame = preflight.frame
        accepted = bool(len(frame) >= 5 and frame["close"].iloc[-1] >= frame["close"].iloc[-5])
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            72.0 if accepted else 0.0,
            "Post-earnings/fundamental catalyst with price holding above recent levels."
            if accepted
            else "No recent earnings/fundamental catalyst with continuation structure.",
            {"preflight": preflight.payload, "catalyst_id": catalyst.id, "rows": len(frame)},
        )


class OpeningRangeBreakoutScanner(ProductionStrategyScanner):
    strategy_id = "OPENING_RANGE_BREAKOUT"
    timeframe = "1Min"

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        frame = preflight.frame
        if len(frame) < 30:
            return self.engine._decision(
                self.strategy_id,
                self.strategy_id,
                symbol,
                False,
                0.0,
                "Not enough intraday candles for opening range breakout.",
                {"preflight": preflight.payload, "rows": len(frame), "catalyst_id": catalyst.id if catalyst else None},
            )
        opening_high = float(frame["high"].iloc[:15].max())
        latest = float(frame["close"].iloc[-1])
        volume_ok = float(frame["volume"].iloc[-1]) > max(1.0, float(frame["volume"].tail(30).mean()))
        accepted = latest > opening_high and volume_ok
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            70.0 if accepted else 0.0,
            "Price broke above opening range with volume confirmation."
            if accepted
            else "Opening range breakout conditions not met.",
            {
                "preflight": preflight.payload,
                "opening_high": opening_high,
                "latest_close": latest,
                "volume_ok": volume_ok,
                "catalyst_id": catalyst.id if catalyst else None,
            },
        )


class NewsMomentumScanner(ProductionStrategyScanner):
    strategy_id = "NEWS_MOMENTUM"
    timeframe = "1Min"
    catalyst_types = {"news_momentum"}
    requires_catalyst = True

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        frame = preflight.frame
        accepted = bool(len(frame) >= 2 and frame["close"].iloc[-1] > frame["close"].iloc[-2])
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            75.0 if accepted else 0.0,
            "Fresh bullish news catalyst with positive price momentum."
            if accepted
            else "No fresh bullish news momentum structure.",
            {"preflight": preflight.payload, "catalyst_id": catalyst.id, "rows": len(frame)},
        )


class CatalystRunUpScanner(ProductionStrategyScanner):
    strategy_id = "CATALYST_RUN_UP"
    timeframe = "1D"
    requires_catalyst = True

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        frame = preflight.frame
        accepted = bool(catalyst.materiality_score >= 60 and len(frame) >= 20)
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            68.0 if accepted else 0.0,
            "Material catalyst context exists for run-up watchlist."
            if accepted
            else "No sufficiently material catalyst run-up context.",
            {"preflight": preflight.payload, "catalyst_id": catalyst.id},
        )


class RelativeStrengthScanner(ProductionStrategyScanner):
    strategy_id = "RELATIVE_STRENGTH"
    timeframe = "1Min"

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        feature = self.engine._latest_feature_snapshot(symbol)
        rs = None
        if feature and isinstance(feature.snapshot, dict):
            rs = feature.snapshot.get("relative_strength_20d")
        accepted = rs is not None and rs > 2.0
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            min(100.0, 60.0 + float(rs or 0.0)) if accepted else 0.0,
            "Symbol shows positive 20-period relative strength."
            if accepted
            else "Relative strength threshold not met.",
            {
                "preflight": preflight.payload,
                "relative_strength_20d": rs,
                "catalyst_id": catalyst.id if catalyst else None,
            },
        )


class SectorLeadershipScanner(ProductionStrategyScanner):
    strategy_id = "SECTOR_LEADERSHIP"
    timeframe = "1D"

    def _evaluate(self, symbol: str, preflight: ScannerPreflight, catalyst) -> dict:
        row = self.engine.repository.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol)
        )
        feature = self.engine._latest_feature_snapshot(symbol)
        trend_score = feature.snapshot.get("trend_score") if feature and isinstance(feature.snapshot, dict) else None
        accepted = bool(row and row.sector and trend_score is not None and trend_score >= 75)
        return self.engine._decision(
            self.strategy_id,
            self.strategy_id,
            symbol,
            accepted,
            float(trend_score or 0.0),
            "Symbol is a high-trend candidate inside an assigned sector."
            if accepted
            else "Sector leadership trend threshold not met.",
            {
                "preflight": preflight.payload,
                "sector": row.sector if row else None,
                "trend_score": trend_score,
                "catalyst_id": catalyst.id if catalyst else None,
            },
        )


PRODUCTION_SCANNER_CLASSES = (
    VwapReclaimProductionScanner,
    PostEarningsContinuationScanner,
    OpeningRangeBreakoutScanner,
    NewsMomentumScanner,
    CatalystRunUpScanner,
    RelativeStrengthScanner,
    SectorLeadershipScanner,
)


class ProductionScannerEngine:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self._preflight_kill_switches: set[tuple[str, str | None, str]] = set()
        self.scanners = [scanner_class(self) for scanner_class in PRODUCTION_SCANNER_CLASSES]
        registered_ids = tuple(scanner.strategy_id for scanner in self.scanners)
        if registered_ids != REQUIRED_STRATEGY_IDS:
            raise RuntimeError(f"Production scanners are not aligned with required strategies: {registered_ids}")

    def run_once(self, symbols: list[str] | None = None) -> ProductionScannerRunResult:
        self._preflight_kill_switches = set()
        symbols = [item.upper() for item in (symbols or self.repository.active_symbols())]
        accepted = rejected = scanners_run = 0
        for symbol in symbols:
            for scanner in self.scanners:
                decision = scanner.scan(symbol)
                scanners_run += 1
                if decision["accepted"]:
                    accepted += 1
                else:
                    rejected += 1
                self.repository.store_generic_scanner_result(
                    scanner_name=decision["scanner_name"],
                    scanner_rule_version=PRODUCTION_SCANNER_VERSION,
                    symbol=symbol,
                    strategy_id=decision["strategy_id"],
                    accepted=decision["accepted"],
                    score=decision["score"],
                    reason=decision["reason"],
                    payload=decision["payload"],
                    source_timestamp=decision["source_timestamp"],
                )
        return ProductionScannerRunResult(
            symbols_seen=len(symbols),
            scanners_run=scanners_run,
            accepted=accepted,
            rejected=rejected,
            reason="Production scanner framework evaluated all configured scanners.",
        )

    def _vwap_reclaim(self, symbol: str) -> dict:
        strategy_id = "VWAP_RECLAIM"
        preflight = self._preflight(symbol, strategy_id, "1Min")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        frame = preflight.frame
        if len(frame) < 2 or "vwap" not in frame or frame["vwap"].tail(2).isna().any():
            return self._decision(
                strategy_id,
                strategy_id,
                symbol,
                False,
                0.0,
                "Not enough intraday VWAP candles for reclaim scan.",
                {"preflight": preflight.payload, "rows": len(frame)},
            )
        previous_close = float(frame["close"].iloc[-2])
        previous_vwap = float(frame["vwap"].iloc[-2])
        latest_close = float(frame["close"].iloc[-1])
        latest_vwap = float(frame["vwap"].iloc[-1])
        average_volume = max(1.0, float(frame["volume"].tail(min(30, len(frame))).mean()))
        relative_volume = float(frame["volume"].iloc[-1]) / average_volume
        catalyst = self._latest_catalyst(symbol, None)
        feature = self._latest_feature_snapshot(symbol)
        relative_strength = (
            feature.snapshot.get("relative_strength_20d")
            if feature and isinstance(feature.snapshot, dict)
            else None
        )
        reclaim = previous_close < previous_vwap and latest_close > latest_vwap
        volume_ok = relative_volume > 1.5
        confirmation = bool(catalyst) or (relative_strength is not None and float(relative_strength) > 2.0)
        accepted = reclaim and volume_ok and confirmation
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            min(100.0, 62.0 + min(relative_volume, 4.0) * 6.0 + (8.0 if bool(catalyst) else 0.0))
            if accepted
            else 0.0,
            "VWAP reclaim from below confirmed with volume and catalyst/relative-strength support."
            if accepted
            else "VWAP reclaim conditions not met.",
            {
                "preflight": preflight.payload,
                "previous_close": previous_close,
                "previous_vwap": previous_vwap,
                "latest_close": latest_close,
                "latest_vwap": latest_vwap,
                "relative_volume": relative_volume,
                "catalyst_id": catalyst.id if catalyst else None,
                "relative_strength_20d": relative_strength,
            },
        )

    def _post_earnings_continuation(self, symbol: str) -> dict:
        strategy_id = "POST_EARNINGS_CONTINUATION"
        preflight = self._preflight(symbol, strategy_id, "1D")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        catalyst = self._latest_catalyst(symbol, {"earnings_or_fundamental_filing", "material_filing"})
        frame = preflight.frame
        accepted = bool(catalyst and len(frame) >= 5 and frame["close"].iloc[-1] >= frame["close"].iloc[-5])
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            72.0 if accepted else 0.0,
            "Post-earnings/fundamental catalyst with price holding above recent levels."
            if accepted
            else "No recent earnings/fundamental catalyst with continuation structure.",
            {
                "preflight": preflight.payload,
                "catalyst_id": catalyst.id if catalyst else None,
                "rows": len(frame),
            },
        )

    def _opening_range_breakout(self, symbol: str) -> dict:
        strategy_id = "OPENING_RANGE_BREAKOUT"
        preflight = self._preflight(symbol, strategy_id, "1Min")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        frame = preflight.frame
        if len(frame) < 30:
            return self._decision(
                strategy_id,
                strategy_id,
                symbol,
                False,
                0.0,
                "Not enough intraday candles for opening range breakout.",
                {"preflight": preflight.payload, "rows": len(frame)},
            )
        opening_high = float(frame["high"].iloc[:15].max())
        latest = float(frame["close"].iloc[-1])
        volume_ok = float(frame["volume"].iloc[-1]) > max(1.0, float(frame["volume"].tail(30).mean()))
        accepted = latest > opening_high and volume_ok
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            70.0 if accepted else 0.0,
            "Price broke above opening range with volume confirmation."
            if accepted
            else "Opening range breakout conditions not met.",
            {
                "preflight": preflight.payload,
                "opening_high": opening_high,
                "latest_close": latest,
                "volume_ok": volume_ok,
            },
        )

    def _news_momentum(self, symbol: str) -> dict:
        strategy_id = "NEWS_MOMENTUM"
        preflight = self._preflight(symbol, strategy_id, "1Min")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        catalyst = self._latest_catalyst(symbol, {"news_momentum"})
        frame = preflight.frame
        accepted = bool(catalyst and len(frame) >= 2 and frame["close"].iloc[-1] > frame["close"].iloc[-2])
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            75.0 if accepted else 0.0,
            "Fresh bullish news catalyst with positive price momentum."
            if accepted
            else "No fresh bullish news momentum structure.",
            {"preflight": preflight.payload, "catalyst_id": catalyst.id if catalyst else None, "rows": len(frame)},
        )

    def _catalyst_run_up(self, symbol: str) -> dict:
        strategy_id = "CATALYST_RUN_UP"
        preflight = self._preflight(symbol, strategy_id, "1D")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        catalyst = self._latest_catalyst(symbol, None)
        frame = preflight.frame
        accepted = bool(catalyst and catalyst.materiality_score >= 60 and len(frame) >= 20)
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            68.0 if accepted else 0.0,
            "Material catalyst context exists for run-up watchlist."
            if accepted
            else "No sufficiently material catalyst run-up context.",
            {"preflight": preflight.payload, "catalyst_id": catalyst.id if catalyst else None},
        )

    def _relative_strength(self, symbol: str) -> dict:
        strategy_id = "RELATIVE_STRENGTH"
        preflight = self._preflight(symbol, strategy_id, "1Min")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        feature = self._latest_feature_snapshot(symbol)
        rs = None
        if feature and isinstance(feature.snapshot, dict):
            rs = feature.snapshot.get("relative_strength_20d")
        accepted = rs is not None and rs > 2.0
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            min(100.0, 60.0 + float(rs or 0.0)) if accepted else 0.0,
            "Symbol shows positive 20-period relative strength."
            if accepted
            else "Relative strength threshold not met.",
            {"preflight": preflight.payload, "relative_strength_20d": rs},
        )

    def _sector_leadership(self, symbol: str) -> dict:
        strategy_id = "SECTOR_LEADERSHIP"
        preflight = self._preflight(symbol, strategy_id, "1D")
        if not preflight.allowed:
            return self._blocked_decision(strategy_id, symbol, preflight)
        row = self.repository.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol)
        )
        feature = self._latest_feature_snapshot(symbol)
        trend_score = feature.snapshot.get("trend_score") if feature and isinstance(feature.snapshot, dict) else None
        accepted = bool(row and row.sector and trend_score is not None and trend_score >= 75)
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            accepted,
            float(trend_score or 0.0),
            "Symbol is a high-trend candidate inside an assigned sector."
            if accepted
            else "Sector leadership trend threshold not met.",
            {"preflight": preflight.payload, "sector": row.sector if row else None, "trend_score": trend_score},
        )

    def _blocked_decision(self, strategy_id: str, symbol: str, preflight: ScannerPreflight) -> dict:
        self._handle_preflight_safety_event(strategy_id=strategy_id, symbol=symbol, preflight=preflight)
        return self._decision(
            strategy_id,
            strategy_id,
            symbol,
            False,
            0.0,
            preflight.reason,
            {"preflight": preflight.payload},
        )

    def _handle_preflight_safety_event(self, *, strategy_id: str, symbol: str, preflight: ScannerPreflight) -> None:
        event_type = None
        if preflight.reason == "Clean Alpaca market data is stale for scanner timeframe.":
            event_type = "STALE_MARKET_DATA"
        elif preflight.reason == "Alpaca market-data provider health is stale.":
            event_type = "STALE_PROVIDER_HEALTH"
        elif preflight.reason == "Market regime snapshot is stale.":
            event_type = "STALE_MARKET_REGIME"
        if not event_type:
            return
        timeframe = preflight.payload.get("timeframe")
        key = (symbol, timeframe, event_type)
        if key in self._preflight_kill_switches:
            return
        self._preflight_kill_switches.add(key)
        self.repository.activate_kill_switch(
            event_type=event_type,
            reason=preflight.reason,
            payload={
                "symbol": symbol,
                "strategy_id": strategy_id,
                "timeframe": timeframe,
                "provider": preflight.payload.get("provider"),
                "latest_data_timestamp": preflight.payload.get("latest_data_timestamp"),
            },
        )

    def _decision(
        self,
        scanner_name: str,
        strategy_id: str,
        symbol: str,
        accepted: bool,
        score: float,
        reason: str,
        payload: dict,
    ) -> dict:
        return {
            "scanner_name": scanner_name,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "accepted": accepted,
            "score": score,
            "reason": reason,
            "payload": payload,
            "source_timestamp": datetime.now(UTC),
        }

    def _strategy_requires_catalyst(self, strategy_id: str, default: bool) -> bool:
        strategy = self._strategy(strategy_id)
        configured_gates = getattr(strategy, "required_preflight_gates", None) if strategy else None
        if isinstance(configured_gates, dict):
            return bool(
                configured_gates.get("catalyst")
                or configured_gates.get("requires_catalyst")
                or "catalyst" in {str(item).lower() for item in configured_gates.get("gates", [])}
            )
        if isinstance(configured_gates, (list, tuple, set)):
            return "catalyst" in {str(item).lower() for item in configured_gates}
        return default or strategy_id in CATALYST_REQUIRED_STRATEGY_IDS

    def _preflight(self, symbol: str, strategy_id: str, timeframe: str) -> ScannerPreflight:
        now = datetime.now(UTC)
        frame, provider = self._best_frame(symbol, timeframe)
        symbol_row = self._symbol_row(symbol)
        strategy = self._strategy(strategy_id)
        regime = self._latest_regime()
        provider_health = self.repository.latest_provider_health_for(PRODUCTION_DATA_PROVIDER)
        cooldown = self.repository.active_strategy_cooldown(
            symbol=symbol,
            strategy_id=strategy_id,
            now=now,
        )
        latest_ts = self._latest_frame_timestamp(frame)
        payload: dict[str, Any] = {
            "symbol": model_to_dict(symbol_row) if symbol_row else None,
            "strategy": model_to_dict(strategy) if strategy else None,
            "provider": provider,
            "provider_health": model_to_dict(provider_health) if provider_health else None,
            "timeframe": timeframe,
            "latest_data_timestamp": latest_ts.isoformat() if latest_ts else None,
            "regime": model_to_dict(regime) if regime else None,
            "cooldown": model_to_dict(cooldown) if cooldown else None,
        }

        blocked_reason = self._preflight_blocker(
            symbol_row=symbol_row,
            strategy=strategy,
            provider=provider,
            provider_health=provider_health,
            frame=frame,
            latest_ts=latest_ts,
            timeframe=timeframe,
            regime=regime,
            cooldown=cooldown,
            now=now,
        )
        if blocked_reason:
            return ScannerPreflight(False, blocked_reason, payload, frame, provider)
        return ScannerPreflight(True, "Scanner preflight gates passed.", payload, frame, provider)

    def _preflight_blocker(
        self,
        *,
        symbol_row,
        strategy,
        provider: str | None,
        provider_health,
        frame,
        latest_ts: datetime | None,
        timeframe: str,
        regime,
        cooldown,
        now: datetime,
    ) -> str | None:
        if not symbol_row or not symbol_row.is_active:
            return "Symbol is not active in the production universe."
        if not symbol_row.is_tradable:
            return f"Symbol is not tradable: {symbol_row.tradability_reason or 'no reason recorded'}"
        if not strategy:
            return "Strategy is not registered."
        if strategy.status not in SCANNER_APPROVED_STATUSES:
            return f"Strategy approval status {strategy.status} is not allowed for production scanning."
        if cooldown:
            return f"Strategy cooldown active until {cooldown.cooldown_until.isoformat()}: {cooldown.reason}"
        symbol = symbol_row.symbol if symbol_row else ""
        if symbol and self.repository.recent_accepted_scanner_emission(
            symbol=symbol,
            strategy_id=strategy.strategy_id if strategy else "",
            within_minutes=SCANNER_TRIGGER_COOLDOWN_MINUTES,
            now=now,
        ):
            return (
                f"Recent accepted scanner result within {SCANNER_TRIGGER_COOLDOWN_MINUTES} minutes "
                "blocks duplicate scanner emission."
            )
        if not provider_health:
            return "Alpaca market-data provider health is missing."
        if provider_health.status != ProviderHealthStatus.HEALTHY.value:
            return f"Alpaca market-data provider health is {provider_health.status}."
        if not self._timestamp_fresh(
            provider_health.source_timestamp,
            max_age_seconds=self.settings.provider_health_max_age_seconds,
            now=now,
        ):
            return "Alpaca market-data provider health is stale."
        if provider != PRODUCTION_DATA_PROVIDER:
            return "Production scanner requires fresh Alpaca market data; Yahoo remains research-only."
        if frame is None or frame.empty or latest_ts is None:
            return "No clean Alpaca market data is available for scanner timeframe."
        if not self._timestamp_fresh(
            latest_ts,
            max_age_seconds=self._freshness_seconds_for_timeframe(timeframe),
            now=now,
        ):
            return "Clean Alpaca market data is stale for scanner timeframe."
        if not regime:
            return "Market regime snapshot is missing."
        if not self._timestamp_fresh(
            regime.source_timestamp,
            max_age_seconds=max(self.settings.scheduler_regime_seconds * 3, self.settings.bar_freshness_max_seconds),
            now=now,
        ):
            return "Market regime snapshot is stale."
        allowed_regimes = set(strategy.allowed_regimes or [])
        if allowed_regimes and regime.market_regime not in allowed_regimes:
            return f"Market regime {regime.market_regime} is not allowed for strategy {strategy.strategy_id}."
        return None

    def _best_frame(self, symbol: str, timeframe: str):
        for provider in ["alpaca_market_data", "yahoo_chart"]:
            frame = self.repository.clean_candles_df(symbol, timeframe=timeframe, provider=provider, limit=500)
            if not frame.empty:
                return frame, provider
        return self.repository.clean_candles_df(symbol, timeframe=timeframe, provider="yahoo_chart", limit=0), None

    def _latest_catalyst(self, symbol: str, types: set[str] | None):
        cutoff = datetime.now(UTC) - timedelta(days=CATALYST_MAX_AGE_DAYS)
        stmt = (
            select(models.Catalyst)
            .where(models.Catalyst.symbol == symbol, models.Catalyst.source_timestamp >= cutoff)
            .order_by(desc(models.Catalyst.created_at))
            .limit(1)
        )
        if types:
            stmt = stmt.where(models.Catalyst.catalyst_type.in_(types))
        return self.repository.session.scalar(stmt)

    def _latest_feature_snapshot(self, symbol: str):
        return self.repository.session.scalar(
            select(models.SymbolFeatureSnapshot)
            .where(models.SymbolFeatureSnapshot.symbol == symbol)
            .order_by(desc(models.SymbolFeatureSnapshot.created_at))
            .limit(1)
        )

    def _symbol_row(self, symbol: str):
        return self.repository.session.scalar(
            select(models.SymbolUniverse).where(models.SymbolUniverse.symbol == symbol)
        )

    def _strategy(self, strategy_id: str):
        return self.repository.session.scalar(
            select(models.StrategyRegistry)
            .where(models.StrategyRegistry.strategy_id == strategy_id)
            .order_by(desc(models.StrategyRegistry.created_at))
            .limit(1)
        )

    def _latest_regime(self):
        return self.repository.session.scalar(
            select(models.MarketRegimeSnapshot)
            .order_by(desc(models.MarketRegimeSnapshot.created_at))
            .limit(1)
        )

    @staticmethod
    def _latest_frame_timestamp(frame) -> datetime | None:
        if frame is None or frame.empty:
            return None
        ts = frame.index[-1]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts

    def _freshness_seconds_for_timeframe(self, timeframe: str) -> int:
        if timeframe == "1D":
            return DAILY_DATA_FRESHNESS_SECONDS
        return self.settings.bar_freshness_max_seconds

    @staticmethod
    def _timestamp_fresh(timestamp: datetime | None, *, max_age_seconds: int, now: datetime) -> bool:
        if not timestamp:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp >= now - timedelta(seconds=max_age_seconds)
