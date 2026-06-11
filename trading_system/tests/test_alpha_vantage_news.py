from __future__ import annotations

from typing import Any

import requests
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.data.collectors.alpha_vantage_news import AlphaVantageNewsCollector
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeHttp:
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self.response or _FakeResponse(payload={"feed": []})


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _seeded_repo() -> TradingRepository:
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    return repo


def _article(
    *,
    title: str,
    url: str,
    tickers: list[dict[str, Any]],
    published: str = "20260603T143000",
    source: str = "Example Wire",
) -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "time_published": published,
        "summary": f"{title} story body.",
        "source": source,
        "overall_sentiment_label": "Bullish",
        "ticker_sentiment": tickers,
    }


def _ts(ticker: str, *, relevance: str = "0.9", sentiment: str = "0.4") -> dict[str, Any]:
    return {
        "ticker": ticker,
        "relevance_score": relevance,
        "ticker_sentiment_score": sentiment,
        "ticker_sentiment_label": "Bullish",
    }


def test_alpha_vantage_collector_stores_and_flags_duplicates():
    repo = _seeded_repo()
    payload = {
        "items": "2",
        "feed": [
            _article(
                title="AMD reportedly wins a large AI customer",
                url="https://x/amd-1",
                tickers=[_ts("AMD"), _ts("NVDA", relevance="0.2")],
            ),
            _article(
                title="AMD reportedly wins a large AI customer",
                url="https://x/amd-2",
                tickers=[_ts("AMD")],
                published="20260603T143100",
            ),
        ],
    }
    http = _FakeHttp(_FakeResponse(payload=payload))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD"])

    assert result.success is True
    assert result.headlines_seen == 2
    assert result.clean_news_stored == 2
    assert result.duplicates_seen == 1
    assert result.rumors_seen == 2  # "reportedly" triggers the rumor heuristic.

    # A single broad call is issued with no per-ticker filter.
    assert len(http.calls) == 1
    _, kwargs = http.calls[0]
    params = kwargs["params"]
    assert "tickers" not in params
    assert params["function"] == "NEWS_SENTIMENT"
    assert params["sort"] == "LATEST"
    assert params["apikey"] == "demo-key"

    row = repo.latest_clean_news(1)[0]
    assert row["duplicate_headline"] is True
    assert row["symbol"] == "AMD"


def test_alpha_vantage_collector_uses_single_broad_call():
    repo = _seeded_repo()
    payload = {
        "feed": [
            _article(title="AMD ships new GPU", url="https://x/amd", tickers=[_ts("AMD")]),
            _article(title="NVDA beats earnings", url="https://x/nvda", tickers=[_ts("NVDA")]),
        ]
    }
    http = _FakeHttp(_FakeResponse(payload=payload))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD", "NVDA"])

    assert result.success is True
    assert len(http.calls) == 1
    assert "tickers" not in http.calls[0][1]["params"]
    assert result.clean_news_stored == 2
    stored_symbols = {row["symbol"] for row in repo.latest_clean_news(5)}
    assert stored_symbols == {"AMD", "NVDA"}


def test_alpha_vantage_collector_fans_out_and_filters_to_active_universe():
    repo = _seeded_repo()
    payload = {
        "feed": [
            _article(
                title="Sector rally lifts chipmakers",
                url="https://x/rally",
                tickers=[_ts("AMD"), _ts("NVDA"), _ts("ZZZZ")],
            )
        ]
    }
    http = _FakeHttp(_FakeResponse(payload=payload))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD", "NVDA"])

    assert result.success is True
    assert result.headlines_seen == 1
    # One raw article, two universe-matched clean rows; ZZZZ is filtered out.
    assert result.raw_news_stored == 1
    assert result.clean_news_stored == 2
    stored_symbols = {row["symbol"] for row in repo.latest_clean_news(5)}
    assert stored_symbols == {"AMD", "NVDA"}


def test_alpha_vantage_collector_skips_article_with_no_universe_ticker():
    repo = _seeded_repo()
    payload = {
        "feed": [
            _article(title="Penny stock pumps", url="https://x/zzz", tickers=[_ts("ZZZZ")])
        ]
    }
    http = _FakeHttp(_FakeResponse(payload=payload))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD"])

    assert result.success is True
    assert result.headlines_seen == 1
    assert result.raw_news_stored == 0
    assert result.clean_news_stored == 0


def test_alpha_vantage_collector_persists_sentiment_and_relevance():
    repo = _seeded_repo()
    payload = {
        "feed": [
            _article(
                title="AMD lands cloud deal",
                url="https://x/amd-deal",
                tickers=[_ts("AMD", relevance="0.83", sentiment="0.51")],
            )
        ]
    }
    http = _FakeHttp(_FakeResponse(payload=payload))

    AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD"])

    row = repo.latest_clean_news(1)[0]
    assert row["symbol"] == "AMD"
    assert abs(row["sentiment_score"] - 0.51) < 1e-6
    assert abs(row["relevance_score"] - 0.83) < 1e-6


def test_alpha_vantage_collector_requires_api_key():
    repo = _seeded_repo()
    http = _FakeHttp(_FakeResponse(payload={}))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key=""), http=http
    ).collect(["AMD"])

    assert result.success is False
    assert "api key" in result.reason.lower()
    assert http.calls == []


def test_alpha_vantage_collector_surfaces_rate_limit_note():
    repo = _seeded_repo()
    note = {
        "Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
    }
    http = _FakeHttp(_FakeResponse(payload=note))

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD"])

    assert result.success is False
    assert result.clean_news_stored == 0
    assert "rate limit" in result.reason.lower()


def test_alpha_vantage_collector_redacts_api_key_in_errors():
    repo = _seeded_repo()
    secret = "SUPERSECRETKEY123"

    class _LeakyHttp:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            self.calls += 1
            raise requests.ConnectionError(
                f"Failed to reach https://www.alphavantage.co/query?function=NEWS_SENTIMENT&apikey={secret}"
            )

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key=secret), http=_LeakyHttp()
    ).collect(["AMD"])

    assert result.success is False
    assert secret not in result.reason
    assert "***" in result.reason
    # The persisted api-call log must also be free of the key.
    logged = repo.latest_api_calls(5)
    assert all(secret not in (entry.get("reason") or "") for entry in logged)
