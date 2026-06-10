from __future__ import annotations

import pytest

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import EnvironmentMode


def test_default_environment_is_research(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT_MODE", raising=False)
    get_settings.cache_clear()
    assert get_settings().environment_mode == EnvironmentMode.RESEARCH


def test_live_mode_requires_explicit_future_confirmation(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT_MODE", "live")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Live trading is not wired"):
        get_settings()


def test_live_order_path_is_never_enabled():
    settings = Settings(environment_mode=EnvironmentMode.PAPER)
    assert settings.live_order_path_enabled is False


RANKING_FIELDS = (
    "ranking_weight_scanner",
    "ranking_weight_freshness",
    "ranking_weight_provider",
    "ranking_weight_regime",
    "ranking_weight_catalyst",
    "ranking_weight_relative_strength",
    "ranking_weight_liquidity",
    "ranking_weight_spread",
    "ranking_grade_a_plus_min",
    "ranking_grade_a_min",
    "ranking_grade_b_min",
    "ranking_grade_watch_min",
    "ranking_relative_strength_multiplier",
    "ranking_neutral_component_score",
    "ranking_unknown_provider_reliability",
)

RANKING_ENV_VARS = (
    "RANKING_WEIGHT_SCANNER",
    "RANKING_WEIGHT_FRESHNESS",
    "RANKING_WEIGHT_PROVIDER",
    "RANKING_WEIGHT_REGIME",
    "RANKING_WEIGHT_CATALYST",
    "RANKING_WEIGHT_RELATIVE_STRENGTH",
    "RANKING_WEIGHT_LIQUIDITY",
    "RANKING_WEIGHT_SPREAD",
    "RANKING_GRADE_A_PLUS_MIN",
    "RANKING_GRADE_A_MIN",
    "RANKING_GRADE_B_MIN",
    "RANKING_GRADE_WATCH_MIN",
    "RANKING_RELATIVE_STRENGTH_MULTIPLIER",
    "RANKING_NEUTRAL_COMPONENT_SCORE",
    "RANKING_UNKNOWN_PROVIDER_RELIABILITY",
)


def test_get_settings_ranking_defaults_match_dataclass(monkeypatch):
    # Guard against drift: the env-fallback defaults in get_settings() must match
    # the Settings dataclass defaults so production ranking can't silently use
    # stale weights when no env override is set.
    for var in RANKING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    defaults = Settings()
    for field in RANKING_FIELDS:
        assert getattr(settings, field) == getattr(defaults, field), field


def test_ranking_component_weights_sum_to_100():
    settings = Settings()
    total = (
        settings.ranking_weight_scanner
        + settings.ranking_weight_freshness
        + settings.ranking_weight_provider
        + settings.ranking_weight_regime
        + settings.ranking_weight_catalyst
        + settings.ranking_weight_relative_strength
        + settings.ranking_weight_liquidity
        + settings.ranking_weight_spread
    )
    assert total == 100.0
    assert settings.ranking_unknown_provider_reliability == 75.0

