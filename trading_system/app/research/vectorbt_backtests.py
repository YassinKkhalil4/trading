from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading_system.app.features.calculations import calculate_vwap


BACKTEST_LOGIC_VERSION = "vwap_reclaim_backtest_v1"
SURVIVORSHIP_BIAS_WARNING = (
    "Early S&P 500 backtests are potentially survivorship-biased unless the universe is point-in-time."
)


@dataclass(frozen=True)
class BacktestAssumptions:
    slippage_bps: float = 5.0
    commission_per_share: float = 0.0
    spread_bps: float = 10.0
    entry_delay_bars: int = 1
    survivorship_bias_warning: str = SURVIVORSHIP_BIAS_WARNING


def build_vwap_reclaim_entries(frame: pd.DataFrame, relative_volume: pd.Series) -> pd.Series:
    vwap = calculate_vwap(frame)
    reclaim = (frame["close"].shift(1) < vwap.shift(1)) & (frame["close"] > vwap)
    return reclaim & (relative_volume > 1.5)


def run_vwap_reclaim_backtest(
    frame: pd.DataFrame,
    *,
    relative_volume: pd.Series,
    assumptions: BacktestAssumptions | None = None,
) -> dict:
    assumptions = assumptions or BacktestAssumptions()
    entries = build_vwap_reclaim_entries(frame, relative_volume).shift(
        assumptions.entry_delay_bars
    ).fillna(False)
    exits = frame["close"] < calculate_vwap(frame)

    try:
        import vectorbt as vbt
    except ImportError as exc:
        raise ImportError("vectorbt is required for research backtests. Install requirements.txt.") from exc

    fees = assumptions.commission_per_share
    slippage = (assumptions.slippage_bps + assumptions.spread_bps / 2) / 10_000
    portfolio = vbt.Portfolio.from_signals(
        close=frame["close"],
        entries=entries,
        exits=exits,
        fees=fees,
        slippage=slippage,
        freq="1min",
    )
    return {
        "logic_version": BACKTEST_LOGIC_VERSION,
        "assumptions": assumptions.__dict__,
        "stats": portfolio.stats().to_dict(),
        "survivorship_bias_warning": assumptions.survivorship_bias_warning,
    }

