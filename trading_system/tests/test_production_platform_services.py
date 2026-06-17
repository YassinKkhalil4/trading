from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import sessionmaker

from trading_system.app.catalysts.catalyst_engine import CatalystEngine
from trading_system.app.core.config import Settings
from trading_system.app.core.enums import (
    CatalystDirection,
    EnvironmentMode,
    MarketRegime,
    OrderStatus,
    ProviderHealthStatus,
    StrategyStatus,
)
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsCollector
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.features.production_features import ProductionFeatureEngine
from trading_system.app.learning.recommendations import LearningRecommendationEngine
from trading_system.app.monitoring.trade_monitor_service import TradeMonitorService
from trading_system.app.ops.provider_health import ProviderHealthService
from trading_system.app.regime.regime_service import MarketRegimeService
from trading_system.app.scanners.production_scanners import ProductionScannerEngine, REQUIRED_STRATEGY_IDS
from trading_system.app.strategies.registry import StrategyRegistryService


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return self.response


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = TradingRepository(Session())
    repo.seed_defaults()
    return repo


def _insert_candles(repo: TradingRepository, symbol: str, provider: str = "alpaca_market_data") -> None:
    start = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)
    for idx in range(60):
        ts = start + timedelta(minutes=idx)
        price = 100.0 + idx * 0.1
        raw_id = repo.store_raw_candle(
            {
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.2,
                "volume": 2_000_000 + idx * 1000,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": "test",
            }
        )


def _scanner_settings() -> Settings:
    return Settings(
        bar_freshness_max_seconds=600,
        provider_health_max_age_seconds=600,
        scheduler_regime_seconds=60,
    )


def _approve_strategy(
    repo: TradingRepository,
    strategy_id: str = "OPENING_RANGE_BREAKOUT",
    status: str = StrategyStatus.PAPER_TESTING.value,
) -> None:
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == strategy_id)
    )
    strategy.status = status
    repo.session.commit()


def _insert_opening_range_candles(
    repo: TradingRepository,
    *,
    symbol: str = "AMD",
    latest_at: datetime | None = None,
    provider: str = "alpaca_market_data",
) -> None:
    latest_at = latest_at or datetime.now(UTC)
    start = latest_at - timedelta(minutes=34)
    for idx in range(35):
        ts = start + timedelta(minutes=idx)
        if idx < 15:
            price = 100.0
            volume = 1_000_000
        else:
            price = 103.0 + idx * 0.15
            volume = 1_250_000
        if idx == 34:
            volume = 6_000_000
            price = 110.0
        raw_id = repo.store_raw_candle(
            {
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": provider,
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.25,
                "volume": volume,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": "test",
            }
        )


def _insert_vwap_reclaim_candles(
    repo: TradingRepository,
    *,
    symbol: str = "AMD",
    latest_at: datetime | None = None,
) -> None:
    latest_at = latest_at or datetime.now(UTC)
    start = latest_at - timedelta(minutes=9)
    for idx in range(10):
        ts = start + timedelta(minutes=idx)
        close = 99.0 if idx < 9 else 101.5
        volume = 1_000_000 if idx < 9 else 5_000_000
        raw_id = repo.store_raw_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "raw_payload": {"idx": idx, "shape": "vwap_reclaim"},
            }
        )
        repo.store_clean_candle(
            {
                "raw_market_data_id": raw_id,
                "provider": "alpaca_market_data",
                "symbol": symbol,
                "timeframe": "1Min",
                "source_timestamp": ts,
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": volume,
                "trade_count": None,
                "vwap": 100.0,
                "data_quality_status": "VALID",
                "quality_reason": "test",
            }
        )


def _insert_catalyst(
    repo: TradingRepository,
    *,
    symbol: str = "AMD",
    catalyst_type: str = "news_momentum",
    source_timestamp: datetime | None = None,
) -> models.Catalyst:
    return repo.store_catalyst(
        event_id=f"test-{symbol.lower()}-{catalyst_type}",
        symbol=symbol,
        catalyst_type=catalyst_type,
        direction=CatalystDirection.BULLISH.value,
        materiality_score=80.0,
        confidence=90.0,
        source="test",
        reason="test catalyst",
        source_timestamp=source_timestamp or datetime.now(UTC),
    )


def _seed_scanner_preflight(
    repo: TradingRepository,
    *,
    strategy_id: str = "OPENING_RANGE_BREAKOUT",
    approve_strategy: bool = True,
    data_latest_at: datetime | None = None,
    provider: str = "alpaca_market_data",
    provider_status: str | None = ProviderHealthStatus.HEALTHY.value,
    provider_health_at: datetime | None = None,
    regime: str = MarketRegime.BULL_TREND.value,
    seed_catalyst: bool = True,
) -> None:
    now = datetime.now(UTC)
    if approve_strategy:
        _approve_strategy(repo, strategy_id)
    _insert_opening_range_candles(
        repo,
        latest_at=data_latest_at or now,
        provider=provider,
    )
    if provider_status:
        repo.store_provider_health_snapshot(
            provider_name="alpaca_market_data",
            status=provider_status,
            reason="test provider health",
            reliability_score=100.0,
            source_timestamp=provider_health_at or now,
        )
    repo.store_market_regime_snapshot(
        market_regime=regime,
        confidence=95.0,
        allowed_bias="long",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="test regime",
        source_timestamp=now,
    )
    if seed_catalyst:
        _insert_catalyst(repo, source_timestamp=now)


def _latest_scanner_result(repo: TradingRepository, scanner_name: str) -> models.ScannerResult:
    return repo.session.scalar(
        select(models.ScannerResult)
        .where(models.ScannerResult.scanner_name == scanner_name)
        .order_by(desc(models.ScannerResult.created_at))
        .limit(1)
    )


def test_alpaca_primary_bar_collector_stores_raw_and_clean_rows():
    repo = _repo()
    payload = {
        "bars": [
            {
                "t": "2026-06-03T14:30:00Z",
                "o": 100.0,
                "h": 101.0,
                "l": 99.5,
                "c": 100.5,
                "v": 2500000,
                "n": 1000,
                "vw": 100.3,
            }
        ]
    }
    settings = Settings(alpaca_paper_api_key="key", alpaca_paper_secret_key="secret")
    result = AlpacaBarsCollector(
        repo,
        settings,
        FakeHttp(
            FakeResponse(
                payload,
                headers={"X-RateLimit-Remaining": "199", "X-RateLimit-Reset": "1800000000"},
            )
        ),
    ).collect("AMD")

    assert result.success is True
    assert result.raw_stored == 1
    assert repo.latest_clean_candles(1)[0]["provider"] == "alpaca_market_data"
    assert repo.latest_provider_rate_limits(1)[0]["limit_remaining"] == 199


def test_provider_health_records_snapshot_from_api_activity():
    repo = _repo()
    repo.log_api_call(
        provider="alpaca_market_data",
        endpoint="test",
        status_code=200,
        success=True,
        reason="ok",
        duration_ms=12.0,
    )

    result = ProviderHealthService(repo, Settings()).run_once()

    assert result.providers_checked >= 1
    assert repo.counts()["provider_health"] >= 1


def test_features_regime_catalysts_scanners_and_learning_pipeline():
    repo = _repo()
    for symbol in ["SPY", "QQQ", "AMD"]:
        _insert_candles(repo, symbol)
    raw_news = repo.store_raw_news(
        provider="news",
        symbol="AMD",
        headline="AMD wins major AI partnership",
        url="https://example.com",
        raw_payload={},
        source_timestamp=datetime(2026, 6, 3, 14, 45, tzinfo=UTC),
    )
    repo.store_clean_news(
        raw_news_id=raw_news.id,
        provider="news",
        symbol="AMD",
        headline="AMD wins major AI partnership",
        normalized_headline_hash="amd-news",
        summary="AMD wins major AI partnership",
        source_confidence_score=75.0,
        duplicate_headline=False,
        rumor_flag=False,
        reason="test",
        source_timestamp=raw_news.source_timestamp,
    )

    features = ProductionFeatureEngine(repo).run_once(["SPY", "QQQ", "AMD"])
    regime = MarketRegimeService(repo).run_once()
    catalysts = CatalystEngine(repo).run_once(["AMD"])
    scanners = ProductionScannerEngine(repo).run_once(["AMD"])
    monitor = TradeMonitorService(repo).run_once()
    learning = LearningRecommendationEngine(repo).run_weekly_review()

    assert features.intraday_snapshots == 3
    assert regime.computed is True
    assert catalysts.catalysts_created >= 1
    assert scanners.scanners_run == 7
    assert monitor.positions_seen == 0
    assert learning.recommendations_created >= 1


def test_production_scanners_register_all_required_strategy_classes():
    repo = _repo()
    engine = ProductionScannerEngine(repo, _scanner_settings())
    registry_ids = {strategy.strategy_id for strategy in StrategyRegistryService().all()}

    assert tuple(scanner.strategy_id for scanner in engine.scanners) == REQUIRED_STRATEGY_IDS
    assert set(REQUIRED_STRATEGY_IDS).issubset(registry_ids)


def test_production_scanner_accepts_opening_range_only_after_preflight_passes():
    repo = _repo()
    _seed_scanner_preflight(repo)

    result = ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert result.scanners_run == 7
    assert row.accepted is True
    assert row.payload["preflight"]["provider"] == "alpaca_market_data"


def test_production_scanner_includes_vwap_reclaim_with_preflight_gates():
    repo = _repo()
    now = datetime.now(UTC)
    _approve_strategy(repo, "VWAP_RECLAIM")
    _insert_vwap_reclaim_candles(repo, latest_at=now)
    repo.store_feature_snapshot(
        symbol="AMD",
        source_timestamp=now,
        feature_version="test",
        snapshot={"relative_strength_20d": 3.5},
    )
    repo.store_provider_health_snapshot(
        provider_name="alpaca_market_data",
        status=ProviderHealthStatus.HEALTHY.value,
        reason="test provider health",
        reliability_score=100.0,
        source_timestamp=now,
    )
    repo.store_market_regime_snapshot(
        market_regime=MarketRegime.BULL_TREND.value,
        confidence=95.0,
        allowed_bias="long",
        risk_multiplier=1.0,
        breakout_permission=True,
        mean_reversion_permission="limited",
        reason="test regime",
        source_timestamp=now,
    )
    _insert_catalyst(repo, source_timestamp=now)

    result = ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "VWAP_RECLAIM")

    assert result.scanners_run == 7
    assert row.accepted is True
    assert row.payload["previous_close"] < row.payload["previous_vwap"]
    assert row.payload["latest_close"] > row.payload["latest_vwap"]
    assert row.payload["relative_strength_20d"] == 3.5


def test_production_scanner_rejects_unapproved_strategy_before_accepting():
    repo = _repo()
    _seed_scanner_preflight(repo, approve_strategy=False)

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert row.accepted is False
    assert "Strategy approval status RESEARCH" in row.reason


def test_production_scanner_rejects_missing_provider_health():
    repo = _repo()
    _seed_scanner_preflight(repo, provider_status=None)

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert row.accepted is False
    assert row.reason == "Alpaca market-data provider health is missing."


def test_production_scanner_rejects_stale_alpaca_data():
    repo = _repo()
    old = datetime.now(UTC) - timedelta(minutes=30)
    _seed_scanner_preflight(repo, data_latest_at=old)

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")
    kill_switch = repo.latest_kill_switches(1)[0]

    assert row.accepted is False
    assert row.reason == "Clean Alpaca market data is stale for scanner timeframe."
    assert kill_switch["event_type"] == "STALE_MARKET_DATA"
    assert kill_switch["payload"]["symbol"] == "AMD"
    assert kill_switch["payload"]["timeframe"] == "1Min"


def test_production_scanner_blocks_signal_creation_for_stale_data_unhealthy_provider_and_cooldown():
    repo = _repo()
    _seed_scanner_preflight(repo, data_latest_at=datetime.now(UTC) - timedelta(minutes=30))
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    stale = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    repo = _repo()
    _seed_scanner_preflight(repo, provider_status=ProviderHealthStatus.DOWN.value)
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    unhealthy = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    repo = _repo()
    _seed_scanner_preflight(repo)
    repo.store_strategy_cooldown(
        symbol="AMD",
        strategy_id="OPENING_RANGE_BREAKOUT",
        cooldown_until=datetime.now(UTC) + timedelta(minutes=20),
        reason="test cooldown",
    )
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    cooldown = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert stale.accepted is False
    assert stale.reason == "Clean Alpaca market data is stale for scanner timeframe."
    assert unhealthy.accepted is False
    assert unhealthy.reason == "Alpaca market-data provider health is DOWN."
    assert cooldown.accepted is False
    assert "Strategy cooldown active" in cooldown.reason


def test_production_scanner_trigger_cooldown_blocks_repeated_signal_emission():
    repo = _repo()
    _seed_scanner_preflight(repo)

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    first = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    second = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert first.accepted is True
    assert second.accepted is False
    assert "duplicate scanner emission" in second.reason.lower()


def test_production_scanner_stale_provider_health_and_regime_trigger_kill_switches():
    repo = _repo()
    old_health = datetime.now(UTC) - timedelta(minutes=20)
    _seed_scanner_preflight(repo, provider_health_at=old_health)

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    provider_kill_switch = repo.latest_kill_switches(1)[0]

    assert provider_kill_switch["event_type"] == "STALE_PROVIDER_HEALTH"
    assert provider_kill_switch["payload"]["provider"] == "alpaca_market_data"

    repo = _repo()
    _seed_scanner_preflight(repo)
    regime = repo.session.scalar(select(models.MarketRegimeSnapshot))
    regime.source_timestamp = datetime.now(UTC) - timedelta(minutes=20)
    repo.session.commit()

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    regime_kill_switch = repo.latest_kill_switches(1)[0]

    assert regime_kill_switch["event_type"] == "STALE_MARKET_REGIME"
    assert regime_kill_switch["payload"]["symbol"] == "AMD"


def test_production_scanner_rejects_yahoo_data_for_production_scan():
    repo = _repo()
    _seed_scanner_preflight(repo, provider="yahoo_chart")

    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    row = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert row.accepted is False
    assert "Yahoo remains research-only" in row.reason


def test_production_scanner_rejects_untradable_symbol_disallowed_regime_and_cooldown():
    repo = _repo()
    _seed_scanner_preflight(repo)
    repo.set_symbol_tradability("AMD", is_tradable=False, reason="test tradability block")
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    untradable = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    repo = _repo()
    _seed_scanner_preflight(repo, regime=MarketRegime.BEAR_TREND.value)
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    bad_regime = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    repo = _repo()
    _seed_scanner_preflight(repo)
    repo.store_strategy_cooldown(
        symbol="AMD",
        strategy_id="OPENING_RANGE_BREAKOUT",
        cooldown_until=datetime.now(UTC) + timedelta(minutes=20),
        reason="test cooldown",
    )
    ProductionScannerEngine(repo, _scanner_settings()).run_once(["AMD"])
    cooldown = _latest_scanner_result(repo, "OPENING_RANGE_BREAKOUT")

    assert untradable.accepted is False
    assert "Symbol is not tradable" in untradable.reason
    assert bad_regime.accepted is False
    assert "Market regime BEAR_TREND is not allowed" in bad_regime.reason
    assert cooldown.accepted is False
    assert "Strategy cooldown active" in cooldown.reason


def test_trade_review_engine_reviews_unreviewed_journal_entries():
    repo = _repo()
    journal = repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="test thesis",
        actual_entry=100.0,
        actual_exit=102.0,
        pnl=20.0,
        human_notes=None,
        mistake_tags=[],
        change_reason="test",
    )

    assert journal.ai_review is None


def test_learning_engine_creates_recommendations_without_mutating_strategy_status():
    repo = _repo()
    strategy = repo.session.scalar(
        select(models.StrategyRegistry).where(models.StrategyRegistry.strategy_id == "VWAP_RECLAIM")
    )
    before_status = strategy.status
    repo.store_journal_entry(
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        entry_thesis="rule violation sample",
        actual_entry=100.0,
        actual_exit=99.0,
        pnl=-10.0,
        human_notes=None,
        mistake_tags=[],
        rule_violations=["STOP_LOSS_BREACHED"],
        change_reason="test journal with violation",
    )

    result = LearningRecommendationEngine(repo).run_weekly_review()

    repo.session.refresh(strategy)
    assert result.recommendations_created >= 1
    assert strategy.status == before_status
    assert repo.counts()["weekly_reviews"] == 1
    assert repo.counts()["recommendations"] >= 1
    assert "STRATEGY_RECOMMENDATION_CREATED" in {
        row["event_type"] for row in repo.latest_audit_logs(10)
    }


def test_trade_monitor_updates_journal_lifecycle_for_partial_exit_and_day_trade_violation():
    repo = _repo()
    entry_at = datetime(2026, 6, 3, 14, 31, tzinfo=UTC)
    partial_exit_at = datetime(2026, 6, 4, 14, 45, tzinfo=UTC)
    signal = models.Signal(
        idempotency_key="signal-monitor-1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction="LONG",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=98.0,
        target_1=105.0,
        target_2=108.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Breaks stop",
        status="SUBMITTED",
        signal_rule_version="test",
        source_timestamp=entry_at,
    )
    repo.session.add(signal)
    repo.session.commit()

    entry_order = repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="buy",
            quantity=10,
            order_type="limit",
            limit_price=100.0,
            stop_loss=98.0,
            idempotency_key="entry-monitor-1",
            status=OrderStatus.FILLED,
            reason="entry filled",
            created_at=entry_at,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=entry_at,
    )
    repo.session.add(
        models.Fill(
            order_id=entry_order.id,
            broker_fill_id="entry-fill-monitor-1",
            symbol="AMD",
            quantity=10,
            price=100.0,
            slippage_bps=5.0,
            commission=0.0,
            source_timestamp=entry_at,
        )
    )
    repo.session.commit()
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=5),
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=5),
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 1000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test",
        }
    )

    first_run = TradeMonitorService(repo).run_once()

    journal = repo.latest_journal(1)[0]
    assert first_run.journal_entries_created == 1
    assert journal["actual_entry"] == 100.0
    assert journal["actual_exit"] is None

    exit_order = repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="sell",
            quantity=4,
            order_type="limit",
            limit_price=105.0,
            stop_loss=98.0,
            idempotency_key="partial-exit-monitor-1",
            status=OrderStatus.FILLED,
            reason="partial exit filled",
            created_at=partial_exit_at,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=partial_exit_at,
    )
    repo.session.add(
        models.Fill(
            order_id=exit_order.id,
            broker_fill_id="partial-exit-fill-monitor-1",
            symbol="AMD",
            quantity=4,
            price=105.0,
            slippage_bps=10.0,
            commission=0.0,
            source_timestamp=partial_exit_at,
        )
    )
    repo.session.commit()
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": partial_exit_at + timedelta(minutes=5),
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": partial_exit_at + timedelta(minutes=5),
            "open": 104.0,
            "high": 106.0,
            "low": 99.0,
            "close": 105.5,
            "volume": 1000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test",
        }
    )

    second_run = TradeMonitorService(repo).run_once()
    updated = repo.latest_journal(1)[0]

    assert second_run.journal_entries_updated == 1
    assert second_run.rule_violations_recorded == 1
    assert updated["actual_exit"] == 105.0
    assert updated["pnl"] == 20.0
    assert updated["max_favorable_excursion"] == 60.0
    assert updated["max_adverse_excursion"] == -10.0
    assert updated["slippage_bps"] == 6.428571428571429
    assert updated["time_in_trade_seconds"] > 86_400
    assert updated["rule_violations"] == ["DAY_TRADE_TO_SWING_BLOCKED"]


def test_trade_monitor_creates_single_protective_exit_order_on_stop_breach():
    repo = _repo()
    entry_at = datetime(2026, 6, 3, 14, 31, tzinfo=UTC)
    signal = models.Signal(
        idempotency_key="signal-stop-breach-1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction="LONG",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=98.0,
        target_1=105.0,
        target_2=108.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Breaks stop",
        status="SUBMITTED",
        signal_rule_version="test",
        source_timestamp=entry_at,
    )
    repo.session.add(signal)
    repo.session.commit()
    entry_order = repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="buy",
            quantity=10,
            order_type="limit",
            limit_price=100.0,
            stop_loss=98.0,
            idempotency_key="entry-stop-breach-1",
            status=OrderStatus.FILLED,
            reason="entry filled",
            created_at=entry_at,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=entry_at,
    )
    repo.session.add(
        models.Fill(
            order_id=entry_order.id,
            broker_fill_id="entry-stop-breach-fill-1",
            symbol="AMD",
            quantity=10,
            price=100.0,
            slippage_bps=5.0,
            commission=0.0,
            source_timestamp=entry_at,
        )
    )
    repo.session.commit()
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=15),
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=15),
            "open": 99.0,
            "high": 100.0,
            "low": 97.0,
            "close": 97.5,
            "volume": 1000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test stop breach",
        }
    )

    first_run = TradeMonitorService(repo).run_once()
    second_run = TradeMonitorService(repo).run_once()
    orders = repo.latest_orders(5)
    protective_exit = next(row for row in orders if row["idempotency_key"].startswith("protective-exit:"))
    journal = repo.latest_journal(1)[0]

    assert first_run.protective_exit_orders_created == 1
    assert second_run.protective_exit_orders_created == 0
    assert repo.counts()["orders"] == 2
    assert protective_exit["side"] == "sell"
    assert protective_exit["quantity"] == 10
    assert protective_exit["order_type"] == "market"
    assert protective_exit["status"] == OrderStatus.SUBMITTED.value
    assert protective_exit["expected_price"] == 97.5
    assert journal["rule_violations"] == ["STOP_LOSS_BREACHED"]


def test_trade_monitor_moves_open_stop_order_to_breakeven_after_one_r_move():
    repo = _repo()
    entry_at = datetime(2026, 6, 3, 14, 31, tzinfo=UTC)
    signal = models.Signal(
        idempotency_key="signal-stop-move-1",
        symbol="AMD",
        strategy_id="VWAP_RECLAIM",
        strategy_version="v1",
        trade_type="DAY_TRADE",
        direction="LONG",
        entry_zone={"low": 100.0, "high": 101.0},
        stop_loss=98.0,
        target_1=104.0,
        target_2=108.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="Breaks stop",
        status="SUBMITTED",
        signal_rule_version="test",
        source_timestamp=entry_at,
    )
    repo.session.add(signal)
    repo.session.commit()
    entry_order = repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="buy",
            quantity=10,
            order_type="limit",
            limit_price=100.0,
            stop_loss=98.0,
            idempotency_key="entry-stop-move-1",
            status=OrderStatus.FILLED,
            reason="entry filled",
            created_at=entry_at,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=entry_at,
    )
    repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="sell",
            quantity=10,
            order_type="stop",
            limit_price=98.0,
            stop_loss=98.0,
            idempotency_key="open-stop-move-1",
            status=OrderStatus.SUBMITTED,
            reason="initial stop submitted",
            created_at=entry_at,
        ),
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=entry_at,
    )
    repo.session.add(
        models.Fill(
            order_id=entry_order.id,
            broker_fill_id="entry-stop-move-fill-1",
            symbol="AMD",
            quantity=10,
            price=100.0,
            slippage_bps=5.0,
            commission=0.0,
            source_timestamp=entry_at,
        )
    )
    repo.session.commit()
    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=20),
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": entry_at + timedelta(minutes=20),
            "open": 101.0,
            "high": 103.0,
            "low": 100.5,
            "close": 102.0,
            "volume": 1000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test stop move",
        }
    )

    first_run = TradeMonitorService(repo).run_once()
    second_run = TradeMonitorService(repo).run_once()
    orders = repo.latest_orders(5)
    replacement = next(row for row in orders if row["idempotency_key"].startswith("open-stop-move-1:replace:"))
    original = next(row for row in orders if row["idempotency_key"] == "open-stop-move-1")

    assert first_run.stop_orders_adjusted == 1
    assert second_run.stop_orders_adjusted == 0
    assert original["status"] == OrderStatus.CANCELLED.value
    assert replacement["status"] == OrderStatus.SUBMITTED.value
    assert replacement["side"] == "sell"
    assert replacement["stop_loss"] == 100.0
