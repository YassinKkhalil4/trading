from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from trading_system.app.alpha.expectancy import AlphaExpectancyRefreshService
from trading_system.app.alpha.leadership import SectorLeadershipService
from trading_system.app.alpha.strategies import AlphaStrategyScannerService
from trading_system.app.core.config import Settings
from trading_system.app.core.enums import Direction, EnvironmentMode, TradeType
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.risk.risk_engine import PortfolioState, RiskEngine
from trading_system.app.signals.signal_engine import TradeSignal


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return TradingRepository(session)


def test_alpha_scanner_persists_rejected_candidates_with_reason():
    repo = _repo()
    repo.session.add(
        models.SymbolUniverse(
            symbol="AAPL",
            is_active=True,
            source_timestamp=datetime.now(UTC),
        )
    )
    repo.session.commit()

    result = AlphaStrategyScannerService(repo, Settings()).run_strategy(
        "CATALYST_VWAP_RECLAIM", symbols=["AAPL"]
    )

    assert result.rejected == 1
    scanners = repo.latest_scanner_results(5)
    assert scanners[0]["scanner_name"] == "CATALYST_VWAP_RECLAIM"
    assert scanners[0]["accepted"] is False
    assert repo.latest_alpha_rejections(5)[0]["reason_code"] == "NO_INTRADAY_DATA"


def test_adaptive_risk_sizing_reduces_alpha_context_without_bypassing_hard_rules():
    signal = TradeSignal(
        symbol="AAPL",
        strategy_id="CATALYST_VWAP_RECLAIM",
        strategy_version="v1",
        trade_type=TradeType.DAY_TRADE,
        direction=Direction.LONG,
        entry_zone=(100.0, 100.5),
        stop_loss=99.0,
        target_1=102.0,
        target_2=104.0,
        risk_reward=2.0,
        confidence_score=80.0,
        time_horizon="intraday",
        invalidation="lost vwap",
        source_timestamp=datetime.now(UTC),
        idempotency_key="alpha-risk-test",
    )
    settings = Settings(environment_mode=EnvironmentMode.PAPER, risk_per_trade_pct=1.0)
    decision = RiskEngine(settings).evaluate(
        signal,
        PortfolioState(
            account_equity=10_000.0,
            open_positions=0,
            daily_loss_pct=0.0,
            weekly_loss_pct=0.0,
            sector_exposure_pct=0.0,
            trades_today=0,
            trades_by_strategy_today={},
            opportunity_grade="A",
            expectancy_r=1.2,
            expectancy_sample_size=50,
            market_regime="BULL_TREND",
            annualized_volatility=0.4,
        ),
    )
    assert decision.approved is True
    assert decision.position_size_dollars > 0
    assert decision.annualized_volatility == 0.4
    assert decision.risk_amount == decision.position_size_dollars

    rejected = RiskEngine(settings).evaluate(
        signal,
        PortfolioState(
            account_equity=10_000.0,
            open_positions=0,
            daily_loss_pct=0.0,
            weekly_loss_pct=0.0,
            sector_exposure_pct=0.0,
            trades_today=0,
            trades_by_strategy_today={},
            opportunity_grade="A+",
            expectancy_r=-0.2,
            expectancy_sample_size=100,
            annualized_volatility=0.4,
        ),
    )
    assert rejected.approved is False
    assert "expectancy" in rejected.reason


def test_sector_leadership_and_expectancy_refresh_persist_snapshots():
    repo = _repo()
    now = datetime.now(UTC)
    repo.session.add_all(
        [
            models.SymbolUniverse(
                symbol="AAPL", sector="Technology", is_active=True, source_timestamp=now
            ),
            models.SymbolFeatureSnapshot(
                symbol="AAPL",
                feature_version="test",
                snapshot={"relative_strength_20d": 3.0},
                source_timestamp=now,
            ),
        ]
    )
    repo.session.commit()

    leadership = SectorLeadershipService(repo).refresh()
    expectancy = AlphaExpectancyRefreshService(repo).refresh()

    assert leadership.symbols_scored == 1
    assert repo.latest_sector_strength(5)
    assert repo.latest_symbol_relative_strength(5)
    assert expectancy.snapshots_created >= 1
    assert repo.latest_expectancy_snapshots(5)


def test_missing_alpha_intelligence_layers_persist_and_feed_scanners():
    repo = _repo()
    now = datetime.now(UTC)
    repo.session.add(
        models.SymbolUniverse(
            symbol="GME",
            name="Game Example",
            sector="Consumer Cyclical",
            is_active=True,
            is_tradable=True,
            is_liquid=True,
            dollar_volume=75_000_000,
            raw_asset_payload={
                "short_interest": {
                    "short_interest_pct_float": 32.0,
                    "days_to_cover": 6.0,
                    "borrow_fee_pct": 18.0,
                    "utilization_pct": 92.0,
                    "float_shares": 40_000_000,
                },
                "options": {
                    "iv_rank": 70.0,
                    "iv_percentile": 82.0,
                    "open_interest": 250_000,
                    "gamma_exposure": 2_000_000,
                    "delta_exposure": 1_500_000,
                    "expected_move_pct": 14.0,
                    "weekly_expiry": True,
                },
                "revenue_growth": 65.0,
                "capital_flows_score": 20.0,
                "institutional_accumulation_score": 70.0,
            },
            source_timestamp=now,
        )
    )
    repo.session.add(
        models.CleanNews(
            provider="news",
            symbol="GME",
            headline="GME attracts new narrative and capital flows",
            normalized_headline_hash="gme-narrative",
            source_confidence_score=80.0,
            relevance_score=0.9,
            duplicate_headline=False,
            rumor_flag=False,
            source_timestamp=now,
        )
    )
    # Daily support context for failed-breakdown reversal.
    for idx, (open_, high, low, close) in enumerate([(10, 11, 9, 10), (10, 12, 9.5, 11)]):
        repo.store_clean_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": "GME",
                "timeframe": "1D",
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 2_000_000,
                "trade_count": None,
                "vwap": None,
                "data_quality_status": "VALID",
                "quality_reason": None,
                "source_timestamp": now.replace(day=now.day - 2 + idx),
            }
        )
    for minute in range(5):
        close = 10.8 if minute < 4 else 10.5
        low = 8.5 if minute == 2 else 10.0
        repo.store_clean_candle(
            {
                "provider": "alpaca_market_data",
                "symbol": "GME",
                "timeframe": "1Min",
                "open": 10.0,
                "high": 11.0,
                "low": low,
                "close": close,
                "volume": 100_000 if minute < 4 else 300_000,
                "trade_count": None,
                "vwap": 10.2,
                "data_quality_status": "VALID",
                "quality_reason": None,
                "source_timestamp": now.replace(minute=minute),
            }
        )
    repo.session.commit()

    from trading_system.app.alpha.intelligence import (
        MultiBaggerScoringService,
        OptionsIntelligenceService,
        PointInTimeUniverseService,
        ShortInterestService,
    )

    pit = PointInTimeUniverseService(repo).snapshot_current_universe(as_of=now)
    short = ShortInterestService(repo).refresh_from_universe_payloads(["GME"])
    options = OptionsIntelligenceService(repo).refresh_from_universe_payloads(["GME"])
    multi = MultiBaggerScoringService(repo).score_universe(["GME"])

    assert pit.records_created == 1
    assert repo.point_in_time_universe(as_of=now)[0]["symbol"] == "GME"
    assert short.records_created == 1
    assert repo.latest_short_interest_for("GME").short_score >= 70
    assert options.records_created == 1
    assert repo.latest_options_intelligence_for("GME").options_score > 0
    assert multi.records_created == 1

    scanner = AlphaStrategyScannerService(repo, Settings()).run_strategy(
        "FAILED_BREAKDOWN_REVERSAL", symbols=["GME"]
    )
    assert scanner.accepted == 1
    payload = repo.latest_scanner_results(1)[0]["payload"]
    assert payload["short_score"] >= 70


def test_sector_leadership_uses_actual_sector_etf_vs_spy_when_available():
    repo = _repo()
    now = datetime.now(UTC)
    repo.session.add_all(
        [
            models.SymbolUniverse(
                symbol="MSFT", sector="Technology", is_active=True, source_timestamp=now
            ),
            models.SymbolFeatureSnapshot(
                symbol="MSFT",
                feature_version="test",
                snapshot={"relative_strength_20d": 1.0},
                source_timestamp=now,
            ),
        ]
    )
    repo.session.commit()
    for symbol, closes in {
        "SPY": [100.0, 101.0],
        "XLK": [100.0, 104.0],
        "MSFT": [100.0, 106.0],
    }.items():
        for idx, close in enumerate(closes):
            repo.store_clean_candle(
                {
                    "provider": "alpaca_market_data",
                    "symbol": symbol,
                    "timeframe": "1D",
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000_000,
                    "trade_count": None,
                    "vwap": None,
                    "data_quality_status": "VALID",
                    "quality_reason": None,
                    "source_timestamp": now.replace(day=now.day - 1 + idx),
                }
            )

    SectorLeadershipService(repo).refresh()

    sector = repo.latest_sector_strength(1)[0]
    symbol = repo.latest_symbol_relative_strength(1)[0]
    assert sector["sector_etf"] == "XLK"
    assert sector["payload"]["true_sector_etf_analytics"] is True
    assert sector["sector_vs_spy_score"] == 65.0
    assert symbol["payload"]["actual_stock_vs_sector_score"] == 60.0
