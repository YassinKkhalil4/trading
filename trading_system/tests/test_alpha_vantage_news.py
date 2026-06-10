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
    def __init__(
        self,
        response: _FakeResponse | None = None,
        by_ticker: dict[str, _FakeResponse] | None = None,
    ) -> None:
        self.response = response
        self.by_ticker = by_ticker or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        ticker = kwargs.get("params", {}).get("tickers")
        if ticker in self.by_ticker:
            return self.by_ticker[ticker]
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


def _article(*, title: str, url: str, ticker: str, published: str = "20260603T143000") -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "time_published": published,
        "summary": f"{ticker} story body.",
        "source": "Example Wire",
        "overall_sentiment_label": "Bullish",
        "ticker_sentiment": [
            {"ticker": ticker, "relevance_score": "0.9", "ticker_sentiment_label": "Bullish"},
        ],
    }


def test_alpha_vantage_collector_stores_and_flags_duplicates():
    repo = _seeded_repo()
    payload = {
        "items": "2",
        "feed": [
            {
                **_article(title="AMD reportedly wins a large AI customer", url="https://x/amd-1", ticker="AMD"),
                "ticker_sentiment": [
                    {"ticker": "AMD", "relevance_score": "0.9", "ticker_sentiment_label": "Bullish"},
                    {"ticker": "NVDA", "relevance_score": "0.2", "ticker_sentiment_label": "Neutral"},
                ],
            },
            _article(
                title="AMD reportedly wins a large AI customer",
                url="https://x/amd-2",
                ticker="AMD",
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

    _, kwargs = http.calls[0]
    assert kwargs["params"]["tickers"] == "AMD"
    assert kwargs["params"]["apikey"] == "demo-key"
    assert kwargs["params"]["function"] == "NEWS_SENTIMENT"

    row = repo.latest_clean_news(1)[0]
    assert row["duplicate_headline"] is True
    assert row["symbol"] == "AMD"


def test_alpha_vantage_collector_requests_one_call_per_symbol():
    repo = _seeded_repo()
    http = _FakeHttp(
        by_ticker={
            "AMD": _FakeResponse(
                payload={"feed": [_article(title="AMD ships new GPU", url="https://x/amd", ticker="AMD")]}
            ),
            "NVDA": _FakeResponse(
                payload={"feed": [_article(title="NVDA beats earnings", url="https://x/nvda", ticker="NVDA")]}
            ),
        }
    )

    result = AlphaVantageNewsCollector(
        repo, Settings(alpha_vantage_api_key="demo-key"), http=http
    ).collect(["AMD", "NVDA"])

    assert result.success is True
    assert len(http.calls) == 2
    requested = sorted(call[1]["params"]["tickers"] for call in http.calls)
    assert requested == ["AMD", "NVDA"]
    assert result.clean_news_stored == 2
    stored_symbols = {row["symbol"] for row in repo.latest_clean_news(5)}
    assert stored_symbols == {"AMD", "NVDA"}


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
