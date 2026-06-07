from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_system.app.db.repositories import TradingRepository
from trading_system.app.features.calculations import (
    EXPANDED_FEATURE_VERSION,
    InvalidFeatureData,
    calculate_distance_pct,
    calculate_relative_strength,
    calculate_spread_bps,
    calculate_trend_score,
    calculate_volatility_score,
    calculate_volume_spike_score,
    calculate_vwap,
    compute_core_features,
)


@dataclass(frozen=True)
class FeatureRunResult:
    symbols_seen: int
    intraday_snapshots: int
    daily_snapshots: int
    reason: str
    feature_version: str = EXPANDED_FEATURE_VERSION


class ProductionFeatureEngine:
    def __init__(self, repository: TradingRepository) -> None:
        self.repository = repository

    def run_once(self, symbols: list[str] | None = None) -> FeatureRunResult:
        symbols = symbols or self.repository.active_symbols()
        benchmark = self.repository.clean_candles_df("SPY", provider="alpaca_market_data", limit=100)
        if benchmark.empty:
            benchmark = self.repository.clean_candles_df("SPY", provider="yahoo_chart", limit=100)
        intraday = daily = 0
        for symbol in symbols:
            frame = self._best_frame(symbol, "1Min")
            if len(frame) >= 2:
                try:
                    self._store_intraday(symbol, frame, benchmark)
                    intraday += 1
                except InvalidFeatureData:
                    pass
            daily_frame = self._best_frame(symbol, "1D")
            if len(daily_frame) >= 2:
                self._store_daily(symbol, daily_frame, benchmark)
                daily += 1
        return FeatureRunResult(
            symbols_seen=len(symbols),
            intraday_snapshots=intraday,
            daily_snapshots=daily,
            reason="Production feature snapshots generated from best available clean data.",
        )

    def _best_frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        for provider in ["alpaca_market_data", "yahoo_chart"]:
            frame = self.repository.clean_candles_df(
                symbol,
                timeframe=timeframe,
                provider=provider,
                limit=500,
                valid_only=False,
            )
            if not frame.empty:
                if _has_invalid_candle_status(frame):
                    return pd.DataFrame()
                return frame
        return pd.DataFrame()

    def _store_intraday(self, symbol: str, frame: pd.DataFrame, benchmark: pd.DataFrame) -> None:
        ordered = frame.sort_index()
        latest = ordered.iloc[-1]
        previous = ordered.iloc[-2]
        core = compute_core_features(ordered, previous_close=float(previous["close"]))
        vwap = calculate_vwap(ordered)
        price = float(latest["close"])
        relative_volume = core.relative_volume
        spread_bps = calculate_spread_bps(float(latest["low"]), float(latest["high"]))
        snapshot = {
            "price": price,
            "vwap": core.vwap,
            "atr": core.atr,
            "relative_volume": relative_volume,
            "gap_pct": core.premarket_gap_pct,
            "premarket_gap_pct": core.premarket_gap_pct,
            "volume_spike_score": calculate_volume_spike_score(relative_volume),
            "distance_from_vwap_pct": calculate_distance_pct(price, float(vwap.iloc[-1])),
            "distance_from_20ema_pct": calculate_distance_pct(
                price, float(ordered["close"].ewm(span=20, adjust=False).mean().iloc[-1])
            ),
            "distance_from_50sma_pct": calculate_distance_pct(
                price, float(ordered["close"].rolling(window=50, min_periods=1).mean().iloc[-1])
            ),
            "relative_strength_20d": calculate_relative_strength(ordered.tail(20), benchmark.tail(20))
            if not benchmark.empty
            else None,
            "trend_score": calculate_trend_score(ordered),
            "volatility_score": calculate_volatility_score(ordered),
            "spread_bps": spread_bps,
        }
        self.repository.store_intraday_features(
            symbol=symbol,
            source_timestamp=ordered.index[-1].to_pydatetime(),
            feature_version=EXPANDED_FEATURE_VERSION,
            price=price,
            vwap=snapshot["vwap"],
            atr=snapshot["atr"],
            relative_volume=relative_volume,
            gap_pct=snapshot["gap_pct"],
            volume_spike_score=snapshot["volume_spike_score"],
            liquidity_score=_liquidity_score(ordered),
            spread_score=max(0.0, 100.0 - spread_bps),
        )
        self.repository.store_feature_snapshot(
            symbol=symbol,
            source_timestamp=ordered.index[-1].to_pydatetime(),
            feature_version=EXPANDED_FEATURE_VERSION,
            snapshot=snapshot,
        )

    def _store_daily(self, symbol: str, frame: pd.DataFrame, benchmark: pd.DataFrame) -> None:
        ordered = frame.sort_index()
        latest = ordered.iloc[-1]
        previous = ordered.iloc[-2]
        atr = calculate_atr(ordered).iloc[-1]
        price = float(latest["close"])
        self.repository.store_daily_features(
            symbol=symbol,
            source_timestamp=ordered.index[-1].to_pydatetime(),
            feature_version=EXPANDED_FEATURE_VERSION,
            atr=float(atr),
            atr_pct=(float(atr) / price) * 100 if price > 0 else None,
            gap_pct=calculate_gap_pct(float(latest["open"]), float(previous["close"])),
            trend_score=calculate_trend_score(ordered),
            volatility_score=calculate_volatility_score(ordered),
            liquidity_score=_liquidity_score(ordered),
        )


def _liquidity_score(frame: pd.DataFrame) -> float:
    dollar_volume = float((frame["close"] * frame["volume"]).tail(20).mean())
    return float(max(0.0, min(100.0, dollar_volume / 1_000_000)))


def _has_invalid_candle_status(frame: pd.DataFrame) -> bool:
    if "data_quality_status" not in frame.columns:
        return False
    return bool((frame["data_quality_status"].dropna().astype(str) != "VALID").any())
