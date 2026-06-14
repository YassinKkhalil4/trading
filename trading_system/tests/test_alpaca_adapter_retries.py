from __future__ import annotations

import asyncio

import httpx

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import EnvironmentMode
from trading_system.app.execution.alpaca_live_adapter import AlpacaLiveAdapter
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperAdapter


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload=None, content: bytes = b"{}") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )
            raise error

    def json(self):
        return self._payload


class SequencedHttp:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.posts: list[dict] = []
        self.deletes: list[dict] = []

    async def post(self, _url: str, **kwargs):
        self.posts.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def delete(self, _url: str, **kwargs):
        self.deletes.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_paper_order_submit_retries_transient_failure_with_same_client_order_id():
    http = SequencedHttp(
        [
            httpx.ConnectError("temporary network failure"),
            FakeResponse(payload={"id": "paper-broker-1"}),
        ]
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="paper-key",
        alpaca_paper_secret_key="paper-secret",
        alpaca_order_max_attempts=2,
    )

    result = asyncio.run(
        AlpacaPaperAdapter(settings, http=http).submit_limit_bracket_order(
        symbol="AMD",
        side="buy",
        quantity=10,
        limit_price=100.0,
        stop_price=97.0,
        take_profit_price=110.0,
            client_order_id="paper-client-1",
        )
    )

    assert result.submitted is True
    assert result.broker_order_id == "paper-broker-1"
    assert len(http.posts) == 2
    assert {call["json"]["client_order_id"] for call in http.posts} == {"paper-client-1"}
    assert "2 attempt" in result.reason


def test_live_order_submit_does_not_retry_non_retryable_http_error():
    http = SequencedHttp([FakeResponse(status_code=400, payload={"message": "bad request"})])
    settings = Settings(
        environment_mode=EnvironmentMode.LIVE,
        allow_live_trading=True,
        confirm_live_trading="I_UNDERSTAND_RISK",
        enable_live_order_path=True,
        alpaca_live_api_key="live-key",
        alpaca_live_secret_key="live-secret",
        alpaca_order_max_attempts=3,
    )

    result = asyncio.run(
        AlpacaLiveAdapter(settings, http=http).submit_limit_bracket_order(
        symbol="AMD",
        side="buy",
        quantity=10,
        limit_price=100.0,
        stop_price=97.0,
        take_profit_price=110.0,
            client_order_id="live-client-1",
        )
    )

    assert result.submitted is False
    assert len(http.posts) == 1
    assert result.payload["request"]["client_order_id"] == "live-client-1"


def test_live_cancel_all_retries_retryable_http_error():
    http = SequencedHttp(
        [
            FakeResponse(status_code=503, payload={"message": "try later"}),
            FakeResponse(payload={"accepted": True}),
        ]
    )
    settings = Settings(
        environment_mode=EnvironmentMode.LIVE,
        allow_live_trading=True,
        confirm_live_trading="I_UNDERSTAND_RISK",
        enable_live_order_path=True,
        alpaca_live_api_key="live-key",
        alpaca_live_secret_key="live-secret",
        alpaca_order_max_attempts=2,
    )

    result = asyncio.run(AlpacaLiveAdapter(settings, http=http).cancel_all_orders())

    assert result.success is True
    assert result.payload == {"accepted": True}
    assert len(http.deletes) == 2
    assert "2 attempt" in result.reason


def test_paper_single_order_cancel_retries_retryable_http_error():
    http = SequencedHttp(
        [
            FakeResponse(status_code=503, payload={"message": "try later"}),
            FakeResponse(payload={"cancelled": True}),
        ]
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="paper-key",
        alpaca_paper_secret_key="paper-secret",
        alpaca_order_max_attempts=2,
    )

    result = asyncio.run(
        AlpacaPaperAdapter(settings, http=http).cancel_order("paper-broker-order-1")
    )

    assert result.success is True
    assert result.payload == {"cancelled": True}
    assert len(http.deletes) == 2
    assert "2 attempt" in result.reason
