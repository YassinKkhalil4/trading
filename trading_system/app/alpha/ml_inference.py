from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any

FEATURE_NAMES: tuple[str, ...] = (
    "vwap_distance",
    "relative_volume_5m",
    "spy_correlation_30m",
    "atr_ratio",
)
DEFAULT_ALPHA_MODEL_PATH = Path(os.getenv("ALPHA_MODEL_PATH", "alpha_model_v1.json"))
ALPHA_PROBABILITY_THRESHOLD = 0.65

_model_lock = Lock()
_model: Any | None = None
_model_path: Path | None = None


class AlphaModelNotLoaded(RuntimeError):
    """Raised when alpha inference is requested before the XGBoost model is loaded."""


def load_alpha_model(model_path: str | os.PathLike[str] | None = None) -> Any | None:
    """Load the XGBoost Booster into process memory once and return it."""
    global _model, _model_path
    path = Path(model_path) if model_path is not None else DEFAULT_ALPHA_MODEL_PATH
    with _model_lock:
        if _model is not None and _model_path == path:
            return _model
        if not path.exists():
            _model = None
            _model_path = path
            return None
        try:
            import xgboost as xgb
        except ImportError as exc:  # pragma: no cover - exercised only in incomplete environments
            raise AlphaModelNotLoaded("xgboost is required for alpha model inference.") from exc
        booster = xgb.Booster()
        booster.load_model(str(path))
        _model = booster
        _model_path = path
        return _model


def get_alpha_model() -> Any:
    model = _model if _model is not None else load_alpha_model()
    if model is None:
        raise AlphaModelNotLoaded(f"Alpha model not found at {_model_path or DEFAULT_ALPHA_MODEL_PATH}.")
    return model


def predict_opportunity(features: dict[str, float]) -> float:
    """Return P(y=1) for an alpha opportunity feature vector."""
    missing = [name for name in FEATURE_NAMES if name not in features]
    if missing:
        raise ValueError(f"Missing alpha model features: {', '.join(missing)}")
    values = [[float(features[name]) for name in FEATURE_NAMES]]
    try:
        import xgboost as xgb
    except ImportError as exc:  # pragma: no cover
        raise AlphaModelNotLoaded("xgboost is required for alpha model inference.") from exc
    matrix = xgb.DMatrix(values, feature_names=list(FEATURE_NAMES))
    probability = float(get_alpha_model().predict(matrix)[0])
    return max(0.0, min(1.0, probability))
