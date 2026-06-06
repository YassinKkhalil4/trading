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

