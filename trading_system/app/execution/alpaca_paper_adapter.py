from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.execution.alpaca_retry import request_with_retries


@dataclass(frozen=True)
class AlpacaPaperSyncResult:
    configured: bool
    success: bool
    reason: str
    account: dict[str, Any] | None
    positions: list[dict[str, Any]]
    orders: list[dict[str, Any]]


@dataclass(frozen=True)
class AlpacaPaperOrderResult:
    configured: bool
    submitted: bool
    reason: str
    broker_order_id: str | None
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class AlpacaPaperCancelResult:
    configured: bool
    success: bool
    reason: str
    payload: dict[str, Any] | None


class AlpacaPaperAdapter:
    def __init__(self, settings: Settings | None = None, http: requests.Session | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_paper_api_key and self.settings.alpaca_paper_secret_key)

    def sync(self) -> AlpacaPaperSyncResult:
        if not self.configured:
            return AlpacaPaperSyncResult(
                configured=False,
                success=False,
                reason="Alpaca paper keys are not configured.",
                account=None,
                positions=[],
                orders=[],
            )
        try:
            account = self._get("/v2/account")
            positions = self._get("/v2/positions")
            orders = self._get("/v2/orders", params={"status": "all", "limit": "100"})
        except requests.RequestException as exc:
            return AlpacaPaperSyncResult(
                configured=True,
                success=False,
                reason=f"Alpaca paper sync failed: {exc}",
                account=None,
                positions=[],
                orders=[],
            )
        return AlpacaPaperSyncResult(
            configured=True,
            success=True,
            reason="Alpaca paper account, positions, and orders synced.",
            account=account,
            positions=positions if isinstance(positions, list) else [],
            orders=orders if isinstance(orders, list) else [],
        )

    def submit_limit_bracket_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
        client_order_id: str,
    ) -> AlpacaPaperOrderResult:
        if not self.configured:
            return AlpacaPaperOrderResult(
                configured=False,
                submitted=False,
                reason="Alpaca paper keys are not configured; local paper decision was persisted only.",
                broker_order_id=None,
                payload=None,
            )
        if quantity <= 0:
            return AlpacaPaperOrderResult(
                configured=True,
                submitted=False,
                reason="Quantity must be positive before broker submission.",
                broker_order_id=None,
                payload=None,
            )
        payload = {
            "symbol": symbol,
            "qty": str(quantity),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
            "client_order_id": client_order_id,
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        try:
            def submit_once():
                response = self.http.post(
                    f"{self.settings.alpaca_paper_base_url}/v2/orders",
                    headers=self._headers(),
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = request_with_retries(
                submit_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            data = response.json()
        except requests.RequestException as exc:
            return AlpacaPaperOrderResult(
                configured=True,
                submitted=False,
                reason=f"Alpaca paper order submission failed after bounded retries: {exc}",
                broker_order_id=None,
                payload={"request": payload, "max_attempts": self.settings.alpaca_order_max_attempts},
            )
        return AlpacaPaperOrderResult(
            configured=True,
            submitted=True,
            reason=f"Alpaca paper bracket order submitted after {retry_result.attempts} attempt(s).",
            broker_order_id=str(data.get("id")) if data.get("id") else None,
            payload=data,
        )

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> AlpacaPaperOrderResult:
        if not self.configured:
            return AlpacaPaperOrderResult(
                configured=False,
                submitted=False,
                reason="Alpaca paper keys are not configured; local market order was persisted only.",
                broker_order_id=None,
                payload=None,
            )
        if quantity <= 0:
            return AlpacaPaperOrderResult(
                configured=True,
                submitted=False,
                reason="Quantity must be positive before broker market submission.",
                broker_order_id=None,
                payload=None,
            )
        payload = {
            "symbol": symbol,
            "qty": str(quantity),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        try:
            def submit_once():
                response = self.http.post(
                    f"{self.settings.alpaca_paper_base_url}/v2/orders",
                    headers=self._headers(),
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = request_with_retries(
                submit_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            data = response.json()
        except requests.RequestException as exc:
            return AlpacaPaperOrderResult(
                configured=True,
                submitted=False,
                reason=f"Alpaca paper market order submission failed after bounded retries: {exc}",
                broker_order_id=None,
                payload={"request": payload, "max_attempts": self.settings.alpaca_order_max_attempts},
            )
        return AlpacaPaperOrderResult(
            configured=True,
            submitted=True,
            reason=f"Alpaca paper market order submitted after {retry_result.attempts} attempt(s).",
            broker_order_id=str(data.get("id")) if data.get("id") else None,
            payload=data,
        )

    def cancel_order(self, broker_order_id: str) -> AlpacaPaperCancelResult:
        if not self.configured:
            return AlpacaPaperCancelResult(False, False, "Alpaca paper keys are not configured.", None)
        if not broker_order_id:
            return AlpacaPaperCancelResult(True, False, "Broker order id is required for cancellation.", None)
        try:
            def cancel_once():
                response = self.http.delete(
                    f"{self.settings.alpaca_paper_base_url}/v2/orders/{broker_order_id}",
                    headers=self._headers(),
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = request_with_retries(
                cancel_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            payload = response.json() if getattr(response, "content", b"") else {}
        except requests.RequestException as exc:
            return AlpacaPaperCancelResult(
                True,
                False,
                f"Alpaca paper order cancel failed after bounded retries: {exc}",
                {"broker_order_id": broker_order_id, "max_attempts": self.settings.alpaca_order_max_attempts},
            )
        return AlpacaPaperCancelResult(
            True,
            True,
            f"Alpaca paper order cancel accepted after {retry_result.attempts} attempt(s).",
            payload,
        )

    def _get(self, path: str, params: dict[str, str] | None = None):
        response = self.http.get(
            f"{self.settings.alpaca_paper_base_url}{path}",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_paper_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_paper_secret_key,
        }
