from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import CatalystDirection, MarketRegime, ProviderHealthStatus, StrategyStatus
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.scanners.production_scanners import (
    CATALYST_REQUIRED_STRATEGY_IDS,
    REQUIRED_STRATEGY_IDS,
    ProductionScannerEngine,
)


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _settings() -> Settings:
    return Settings(
        bar_freshness_max_seconds=600,
        provider_health_max_age_seconds=600,
        scheduler_regime_seconds=60,
    )


def _approve_all(repo: TradingRepository) -> None:
    rows = repo.session.scalars(select(models.StrategyRegistry)).all()
    for row in rows:
        row.status = StrategyStatus.PAPER_TESTING.value
    repo.session.commit()


def _store_bar(
    repo: TradingRepository,
    *,
    symbol: str,
    timeframe: str,
    source_timestamp: datetime,
    close: float,
    volume: float,
    vwap: float | None,
) -> None:
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": symbol,
            "timeframe": timeframe,
            "source_timestamp": source_timestamp,
            "raw_payload": {"symbol": symbol, "timeframe": timeframe},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": symbol,
            "timeframe": timeframe,
            "source_timestamp": source_timestamp,
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": volume,
            "trade_count": None,
            "vwap": vwap,
            "data_quality_status": "VALID",
            "quality_reason": "scanner preflight test bar",
        }
    )


def _seed_intraday(repo: TradingRepository, *, latest_at: datetime) -> None:
    start = latest_at - timedelta(minutes=34)
    for idx in range(35):
        close = 99.0
        volume = 1_000_000.0
        if idx == 34:
            close = 110.0
            volume = 8_000_000.0
        _store_bar(
            repo,
            symbol="AMD",
            timeframe="1Min",
            source_timestamp=start + timedelta(minutes=idx),
            close=close,
            volume=volume,
            vwap=100.0,
        )


def _seed_daily(repo: TradingRepository, *, latest_at: datetime) -> None:
    start = latest_at - timedelta(days=24)
    for idx in range(25):
        _store_bar(
            repo,
            symbol="AMD",
            timeframe="1D",
            source_timestamp=start + timedelta(days=idx),
            close=100.0 + idx,
            volume=2_000_000.0,
            vwap=100.0 + idx,
        )


def _seed_common(
    repo: TradingRepository,
    *,
    latest_at: datetime | None = None,
    provider_status: str = ProviderHealthStatus.HEALTHY.value,
    seed_catalysts: bool = False,
) -> datetime:
    now = datetime.now(UTC)
    latest = latest_at or now
    _approve_all(repo)
    _seed_intraday(repo, latest_at=latest)
    _seed_daily(repo, latest_at=latest)
    repo.store_provider_health_snapshot(
        provider_name="alpaca_market_data",
        status=provider_status,
        reason="scanner preflight provider health",
        reliability_score=100.0 if provider_status == ProviderHealthStatus.HEALTHY.value else 0.0,
        source_timestamp=now,
    )
    repo.store_market_regime_snapshot(
        market_regime=MarketRegime.BULL_TREND.value,
        confidence=95.0,
        allowed_bias="long",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="scanner preflight regime",
        source_timestamp=now,
    )
    repo.store_feature_snapshot(
        symbol="AMD",
        source_timestamp=now,
        feature_version="scanner-preflight-test",
        snapshot={"relative_strength_20d": 4.0, "trend_score": 82.0},
    )
    if seed_catalysts:
        _store_catalyst(repo, "news_momentum", now)
        _store_catalyst(repo, "earnings_or_fundamental_filing", now)
        _store_catalyst(repo, "material_filing", now)
    return now


def _store_catalyst(repo: TradingRepository, catalyst_type: str, source_timestamp: datetime) -> None:
    repo.store_catalyst(
        event_id=f"test-amd-{catalyst_type}",
        symbol="AMD",
        catalyst_type=catalyst_type,
        direction=CatalystDirection.BULLISH.value,
        materiality_score=80.0,
        confidence=90.0,
        source="scanner_preflight_test",
        reason="scanner preflight catalyst",
        source_timestamp=source_timestamp,
    )


def _latest_results(repo: TradingRepository) -> dict[str, models.ScannerResult]:
    rows = repo.session.scalars(
        select(models.ScannerResult).order_by(desc(models.ScannerResult.created_at))
    ).all()
    latest: dict[str, models.ScannerResult] = {}
    for row in rows:
        latest.setdefault(row.scanner_name, row)
    return latest


def test_catalyst_required_only_for_catalyst_dependent_scanners():
    repo = _repo()
    _seed_common(repo, seed_catalysts=False)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    results = _latest_results(repo)

    for strategy_id in CATALYST_REQUIRED_STRATEGY_IDS:
        assert results[strategy_id].accepted is False
        assert results[strategy_id].reason == "Required catalyst is missing for strategy preflight."


def test_catalyst_not_required_for_non_catalyst_scanners():
    repo = _repo()
    _seed_common(repo, seed_catalysts=False)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    results = _latest_results(repo)

    non_catalyst = set(REQUIRED_STRATEGY_IDS) - CATALYST_REQUIRED_STRATEGY_IDS
    for strategy_id in non_catalyst:
        assert results[strategy_id].reason != "Required catalyst is missing for strategy preflight."
    assert results["OPENING_RANGE_BREAKOUT"].accepted is True
    assert results["RELATIVE_STRENGTH"].accepted is True
    assert results["SECTOR_LEADERSHIP"].accepted is True


def test_stale_data_blocks_all_scanners():
    repo = _repo()
    _seed_common(repo, latest_at=datetime.now(UTC) - timedelta(days=3), seed_catalysts=True)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    results = _latest_results(repo)

    assert set(results) == set(REQUIRED_STRATEGY_IDS)
    assert all(row.accepted is False for row in results.values())
    assert {row.reason for row in results.values()} == {
        "Clean Alpaca market data is stale for scanner timeframe."
    }


def test_unhealthy_provider_blocks_all_scanners():
    repo = _repo()
    _seed_common(repo, provider_status=ProviderHealthStatus.DOWN.value, seed_catalysts=True)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    results = _latest_results(repo)

    assert set(results) == set(REQUIRED_STRATEGY_IDS)
    assert all(row.accepted is False for row in results.values())
    assert {row.reason for row in results.values()} == {
        "Alpaca market-data provider health is DOWN."
    }


def test_cooldown_blocks_all_scanners():
    repo = _repo()
    now = _seed_common(repo, seed_catalysts=True)
    for strategy_id in REQUIRED_STRATEGY_IDS:
        repo.store_strategy_cooldown(
            symbol="AMD",
            strategy_id=strategy_id,
            cooldown_until=now + timedelta(minutes=20),
            reason="test scanner cooldown",
            source_timestamp=now,
        )

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    results = _latest_results(repo)

    assert set(results) == set(REQUIRED_STRATEGY_IDS)
    assert all(row.accepted is False for row in results.values())
    assert all("Strategy cooldown active" in row.reason for row in results.values())


def test_accepted_scanner_emission_does_not_create_signal_cooldown():
    repo = _repo()
    _seed_common(repo, seed_catalysts=False)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    result = _latest_results(repo)["OPENING_RANGE_BREAKOUT"]
    cooldown = repo.active_strategy_cooldown(
        symbol="AMD",
        strategy_id="OPENING_RANGE_BREAKOUT",
        now=result.source_timestamp,
    )

    assert result.accepted is True
    assert cooldown is None


def test_duplicate_scanner_emission_is_blocked_before_signal_cooldown():
    repo = _repo()
    _seed_common(repo, seed_catalysts=False)

    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    first = _latest_results(repo)["OPENING_RANGE_BREAKOUT"]
    ProductionScannerEngine(repo, _settings()).run_once(["AMD"])
    second = _latest_results(repo)["OPENING_RANGE_BREAKOUT"]

    assert first.accepted is True
    assert second.accepted is False
    assert "duplicate scanner emission" in second.reason.lower()
    assert (
        repo.active_strategy_cooldown(
            symbol="AMD",
            strategy_id="OPENING_RANGE_BREAKOUT",
            now=second.source_timestamp,
        )
        is None
    )
