#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split
from sqlalchemy import create_engine
from xgboost import XGBClassifier

from trading_system.app.core.config import get_settings
from trading_system.app.alpha.ml_inference import FEATURE_NAMES as ALPHA_FEATURE_NAMES

LOOKAHEAD_MINUTES = 15
PROFIT_THRESHOLD = 0.005
MAX_DRAWDOWN = 0.002


def load_clean_market_data(database_url: str) -> pd.DataFrame:
    engine = create_engine(database_url)
    query = """
        SELECT symbol, source_timestamp, close, low, high, volume, vwap
        FROM clean_market_data
        WHERE data_quality_status = 'VALID'
          AND timeframe IN ('1Min', '1m', '1min')
        ORDER BY symbol, source_timestamp
    """
    return pd.read_sql_query(query, engine, parse_dates=["source_timestamp"])


def build_training_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["symbol", "source_timestamp"]).copy()
    spy = frame[frame["symbol"] == "SPY"][["source_timestamp", "close"]].rename(columns={"close": "spy_close"})
    rows: list[pd.DataFrame] = []
    for _, group in frame.groupby("symbol", sort=False):
        group = group.merge(spy, on="source_timestamp", how="left") if group["symbol"].iloc[0] != "SPY" else group.assign(spy_close=group["close"])
        returns = group["close"].pct_change()
        spy_returns = group["spy_close"].pct_change()
        high_low = group["high"] - group["low"]
        group["vwap_distance"] = (group["close"] - group["vwap"]) / group["vwap"]
        group["relative_volume_5m"] = group["volume"] / group["volume"].rolling(5, min_periods=5).mean()
        group["spy_correlation_30m"] = returns.rolling(30, min_periods=15).corr(spy_returns)
        group["atr_ratio"] = high_low.rolling(14, min_periods=5).mean() / group["close"]
        future_max = group["close"].shift(-1).rolling(LOOKAHEAD_MINUTES, min_periods=LOOKAHEAD_MINUTES).max().shift(-(LOOKAHEAD_MINUTES - 1))
        future_min = group["low"].shift(-1).rolling(LOOKAHEAD_MINUTES, min_periods=LOOKAHEAD_MINUTES).min().shift(-(LOOKAHEAD_MINUTES - 1))
        group["target"] = ((future_max >= group["close"] * (1 + PROFIT_THRESHOLD)) & (future_min >= group["close"] * (1 - MAX_DRAWDOWN))).astype(int)
        rows.append(group)
    training = pd.concat(rows, ignore_index=True)
    return training.dropna(subset=[*ALPHA_FEATURE_NAMES, "target"])


def train(database_url: str, output: Path) -> None:
    clean = load_clean_market_data(database_url)
    training = build_training_frame(clean)
    X = training[list(ALPHA_FEATURE_NAMES)]
    y = training["target"]
    X_train, X_valid, y_train, y_valid = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None)
    model = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.85, colsample_bytree=0.85, objective="binary:logistic", eval_metric="logloss", random_state=42)
    model.fit(X_train, y_train, eval_set=[(X_valid, y_valid)], verbose=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    model.get_booster().save_model(str(output))


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the alpha XGBoost binary classifier.")
    parser.add_argument("--database-url", default=get_settings().database_url)
    parser.add_argument("--output", type=Path, default=Path("alpha_model_v1.json"))
    args = parser.parse_args()
    train(args.database_url, args.output)


if __name__ == "__main__":
    main()
