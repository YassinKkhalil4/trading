from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.scanners.news_screener import (
    NEWS_SCREENER_NAME,
    NewsOpportunityScanner,
)


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _activate(repo: TradingRepository, *symbols: str, active: bool = True) -> None:
    for symbol in symbols:
        repo.session.add(
            models.SymbolUniverse(
                symbol=symbol,
                is_active=active,
                is_tradable=True,
                source_timestamp=datetime.now(UTC),
            )
        )
    repo.session.commit()


def _news(
    repo: TradingRepository,
    symbol: str,
    *,
    hash_: str,
    sentiment: float,
    relevance: float,
    confidence: float = 80.0,
    rumor: bool = False,
) -> None:
    now = datetime.now(UTC)
    raw = repo.store_raw_news(
        provider="news",
        symbol=None,
        headline=hash_,
        url=f"https://x/{hash_}",
        raw_payload={"title": hash_},
        source_timestamp=now,
    )
    repo.store_clean_news(
        raw_news_id=raw.id,
        provider="news",
        symbol=symbol,
        headline=hash_,
        normalized_headline_hash=hash_,
        summary=hash_,
        source_confidence_score=confidence,
        duplicate_headline=False,
        rumor_flag=rumor,
        sentiment_score=sentiment,
        relevance_score=relevance,
        reason="seed",
        source_timestamp=now,
    )


def test_news_screener_ranks_and_persists_top_symbols():
    repo = _repo()
    _activate(repo, "AMD", "NVDA")
    # AMD: strong, well-covered, no rumors.
    _news(repo, "AMD", hash_="amd-1", sentiment=0.6, relevance=0.9)
    _news(repo, "AMD", hash_="amd-2", sentiment=0.5, relevance=0.8)
    _news(repo, "AMD", hash_="amd-3", sentiment=0.4, relevance=0.85)
    # NVDA: single low-relevance rumor.
    _news(repo, "NVDA", hash_="nvda-1", sentiment=0.1, relevance=0.2, confidence=35.0, rumor=True)

    result = NewsOpportunityScanner(repo).run_once()

    assert result.symbols_scored == 2
    assert result.results_stored == 2
    assert result.top_symbols[0] == "AMD"

    rows = repo.session.execute(
        select(models.ScannerResult)
        .where(models.ScannerResult.scanner_name == NEWS_SCREENER_NAME)
        .order_by(models.ScannerResult.score.desc())
    ).scalars().all()
    assert [row.symbol for row in rows] == ["AMD", "NVDA"]
    top = rows[0]
    assert top.accepted is True
    assert top.strategy_id == NEWS_SCREENER_NAME
    assert top.payload["news_count"] == 3
    assert top.payload["direction"] == "bullish"
    assert top.score > rows[1].score

    # The screener never writes trade signals.
    assert repo.session.scalar(select(func.count(models.Signal.id))) == 0


def test_news_screener_only_scores_active_universe():
    repo = _repo()
    _activate(repo, "AMD")
    _activate(repo, "DEAD", active=False)
    _news(repo, "AMD", hash_="amd-1", sentiment=0.3, relevance=0.7)
    _news(repo, "DEAD", hash_="dead-1", sentiment=0.9, relevance=0.9)

    result = NewsOpportunityScanner(repo).run_once()

    assert result.symbols_scored == 1
    assert result.top_symbols == ["AMD"]


def test_news_screener_respects_top_n():
    repo = _repo()
    _activate(repo, "AAA", "BBB", "CCC")
    _news(repo, "AAA", hash_="a", sentiment=0.9, relevance=0.9)
    _news(repo, "BBB", hash_="b", sentiment=0.5, relevance=0.6)
    _news(repo, "CCC", hash_="c", sentiment=0.1, relevance=0.3)

    result = NewsOpportunityScanner(repo, top_n=2).run_once()

    assert result.symbols_scored == 3
    assert result.results_stored == 2
    assert result.top_symbols == ["AAA", "BBB"]
