from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


FEATURE_CALCULATION_VERSION = "features_v1"
EXPANDED_FEATURE_VERSION = "features_v2"


@dataclass(frozen=True)
class LiquidityGates:
    min_price: float = 5.0
    min_average_volume: float = 1_000_000
    min_dollar_volume: float = 20_000_000.0
    max_spread_bps: float = 20.0


@dataclass(frozen=True)
class LiquidityDecision:
    passed: bool
    reason: str


@dataclass(frozen=True)
class CoreFeatureSet:
    vwap: float
    atr: float
    premarket_gap_pct: float
    relative_volume: float


class InvalidFeatureData(ValueError):
    pass


def compute_core_features(
    frame: pd.DataFrame,
    *,
    atr_window: int = 14,
    previous_close: float | None = None,
    average_volume: float | None = None,
) -> CoreFeatureSet:
    if frame.empty:
        raise InvalidFeatureData("Cannot compute features without candle data.")
    invalid_statuses = _invalid_quality_statuses(frame)
    if invalid_statuses:
        joined = ", ".join(sorted(invalid_statuses))
        raise InvalidFeatureData(f"Cannot compute features from invalid candle data: {joined}.")

    ordered = frame.sort_index()
    if previous_close is None:
        if len(ordered) < 2:
            raise InvalidFeatureData("Cannot compute premarket gap without a prior close.")
        previous_close = float(ordered["close"].iloc[-2])
    if average_volume is None:
        history = ordered["volume"].iloc[:-1]
        average_volume = float(history.tail(20).mean()) if not history.empty else float(ordered["volume"].mean())

    vwap = calculate_vwap(ordered)
    atr = calculate_atr(ordered, window=atr_window)
    return CoreFeatureSet(
        vwap=float(vwap.iloc[-1]),
        atr=float(atr.iloc[-1]),
        premarket_gap_pct=calculate_gap_pct(float(ordered["open"].iloc[0]), previous_close),
        relative_volume=calculate_relative_volume(float(ordered["volume"].iloc[-1]), float(average_volume)),
    )


def calculate_vwap(frame: pd.DataFrame) -> pd.Series:
    typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3
    cumulative_volume = frame["volume"].cumsum()
    return (typical_price * frame["volume"]).cumsum() / cumulative_volume.replace(0, np.nan)


def calculate_atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    high_low = frame["high"] - frame["low"]
    high_close = (frame["high"] - frame["close"].shift(1)).abs()
    low_close = (frame["low"] - frame["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=window, min_periods=1).mean()


def calculate_gap_pct(open_price: float, previous_close: float) -> float:
    if previous_close <= 0:
        raise ValueError("previous_close must be positive.")
    return ((open_price - previous_close) / previous_close) * 100


def calculate_relative_volume(current_volume: float, average_volume: float) -> float:
    if average_volume <= 0:
        return 0.0
    return current_volume / average_volume


def calculate_volume_spike_score(relative_volume: float) -> float:
    return float(max(0.0, min(100.0, relative_volume * 25.0)))


def calculate_distance_pct(price: float, reference: float | None) -> float | None:
    if reference is None or reference <= 0:
        return None
    return ((price - reference) / reference) * 100


def calculate_trend_score(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    close = frame["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    sma50 = close.rolling(window=50, min_periods=1).mean().iloc[-1]
    price = close.iloc[-1]
    score = 50.0
    score += 20.0 if price > ema20 else -10.0
    score += 20.0 if price > sma50 else -10.0
    if len(close) >= 20:
        score += 10.0 if close.iloc[-1] > close.iloc[-20] else -10.0
    return float(max(0.0, min(100.0, score)))


def calculate_volatility_score(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    atr = calculate_atr(frame).iloc[-1]
    price = frame["close"].iloc[-1]
    if price <= 0:
        return 0.0
    atr_pct = (atr / price) * 100
    return float(max(0.0, min(100.0, atr_pct * 20.0)))


def calculate_relative_strength(symbol_frame: pd.DataFrame, benchmark_frame: pd.DataFrame) -> float | None:
    if len(symbol_frame) < 2 or len(benchmark_frame) < 2:
        return None
    symbol_return = (symbol_frame["close"].iloc[-1] / symbol_frame["close"].iloc[0]) - 1
    benchmark_return = (benchmark_frame["close"].iloc[-1] / benchmark_frame["close"].iloc[0]) - 1
    return float((symbol_return - benchmark_return) * 100)


def calculate_spread_bps(bid: float, ask: float) -> float:
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return float("inf")
    return ((ask - bid) / midpoint) * 10_000


def check_liquidity(
    *,
    price: float,
    average_volume: float,
    dollar_volume: float,
    spread_bps: float,
    gates: LiquidityGates,
) -> LiquidityDecision:
    if price < gates.min_price:
        return LiquidityDecision(False, f"Price {price:.2f} below minimum {gates.min_price:.2f}.")
    if average_volume < gates.min_average_volume:
        return LiquidityDecision(
            False,
            f"Average volume {average_volume:.0f} below minimum {gates.min_average_volume:.0f}.",
        )
    if dollar_volume < gates.min_dollar_volume:
        return LiquidityDecision(
            False,
            f"Dollar volume {dollar_volume:.0f} below minimum {gates.min_dollar_volume:.0f}.",
        )
    if spread_bps > gates.max_spread_bps:
        return LiquidityDecision(
            False,
            f"Spread {spread_bps:.1f} bps above maximum {gates.max_spread_bps:.1f} bps.",
        )
    return LiquidityDecision(True, "Liquidity gates passed.")


def _invalid_quality_statuses(frame: pd.DataFrame) -> set[str]:
    if "data_quality_status" not in frame.columns:
        return set()
    statuses = frame["data_quality_status"].dropna().astype(str)
    return {status for status in statuses if status != "VALID"}
