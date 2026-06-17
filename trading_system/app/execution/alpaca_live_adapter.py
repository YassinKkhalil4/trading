from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.execution.broker_adapter import AbstractBrokerAdapter
from trading_system.app.execution.alpaca_retry import request_with_retries


@dataclass(frozen=True)
class AlpacaLiveSyncResult:
    configured: bool
    success: bool
    reason: str
    account: dict[str, Any] | None
    positions: list[dict[str, Any]]
    orders: list[dict[str, Any]]


@dataclass(frozen=True)
class AlpacaLiveOrderResult:
    configured: bool
    submitted: bool
    reason: str
    broker_order_id: str | None
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class AlpacaLiveEmergencyResult:
    configured: bool
    success: bool
    reason: str
    payload: dict[str, Any] | None


class AlpacaLiveAdapter(AbstractBrokerAdapter):
    def __init__(
        self, settings: Settings | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or httpx.AsyncClient()

    @property
    def configured(self) -> bool:
        return bool(self.settings.alpaca_live_api_key and self.settings.alpaca_live_secret_key)

    async def sync(self) -> AlpacaLiveSyncResult:
        if not self.configured:
            return AlpacaLiveSyncResult(
                configured=False,
                success=False,
                reason="Alpaca live keys are not configured.",
                account=None,
                positions=[],
                orders=[],
            )
        try:
            account = await self._get("/v2/account")
            positions = await self._get("/v2/positions")
            orders = await self._get("/v2/orders", params={"status": "all", "limit": "100"})
        except httpx.HTTPError as exc:
            return AlpacaLiveSyncResult(
                configured=True,
                success=False,
                reason=f"Alpaca live sync failed: {exc}",
                account=None,
                positions=[],
                orders=[],
            )
        return AlpacaLiveSyncResult(
            configured=True,
            success=True,
            reason="Alpaca live account, positions, and orders synced.",
            account=account if isinstance(account, dict) else {},
            positions=positions if isinstance(positions, list) else [],
            orders=orders if isinstance(orders, list) else [],
        )

    async def submit_limit_bracket_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
        client_order_id: str,
    ) -> AlpacaLiveOrderResult:
        if not self.configured:
            return AlpacaLiveOrderResult(
                False, False, "Alpaca live keys are not configured.", None, None
            )
        if quantity <= 0:
            return AlpacaLiveOrderResult(
                True, False, "Quantity must be positive before live submission.", None, None
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

            async def submit_once():
                response = await self.http.post(
                    f"{self.settings.alpaca_live_base_url}/v2/orders",
                    headers=self._headers(),
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                submit_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            data = response.json()
        except httpx.HTTPError as exc:
            return AlpacaLiveOrderResult(
                configured=True,
                submitted=False,
                reason=f"Alpaca live order submission failed after bounded retries: {exc}",
                broker_order_id=None,
                payload={
                    "request": payload,
                    "max_attempts": self.settings.alpaca_order_max_attempts,
                },
            )
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=True,
            reason=f"Alpaca live bracket order submitted after {retry_result.attempts} attempt(s).",
            broker_order_id=str(data.get("id")) if data.get("id") else None,
            payload=data,
        )

    async def latest_bid_ask_midpoint(self, symbol: str) -> float | None:
        if not self.configured:
            return None
        try:
            quote = await self._get(
                f"/v2/stocks/{symbol.strip().upper()}/quotes/latest",
                params={"feed": self.settings.alpaca_primary_data_feed},
                base_url=self.settings.alpaca_live_data_url,
            )
        except httpx.HTTPError:
            return None
        raw_quote = quote.get("quote") if isinstance(quote, dict) else None
        if not isinstance(raw_quote, dict):
            return None
        bid = float(raw_quote.get("bp") or 0)
        ask = float(raw_quote.get("ap") or 0)
        if bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        limit_price: float,
        client_order_id: str,
    ) -> AlpacaLiveOrderResult:
        if not self.configured:
            return AlpacaLiveOrderResult(
                False, False, "Alpaca live keys are not configured.", None, None
            )
        if quantity <= 0 or limit_price <= 0:
            return AlpacaLiveOrderResult(
                True,
                False,
                "Quantity and limit price must be positive before live submission.",
                None,
                None,
            )
        payload = {
            "symbol": symbol,
            "qty": str(quantity),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
            "client_order_id": client_order_id,
        }
        try:

            async def submit_once():
                response = await self.http.post(
                    f"{self.settings.alpaca_live_base_url}/v2/orders",
                    headers=self._headers(),
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                submit_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            data = retry_result.value.json()
        except httpx.HTTPError as exc:
            return AlpacaLiveOrderResult(
                configured=True,
                submitted=False,
                reason=f"Alpaca live limit order submission failed after bounded retries: {exc}",
                broker_order_id=None,
                payload={
                    "request": payload,
                    "max_attempts": self.settings.alpaca_order_max_attempts,
                },
            )
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=True,
            reason=f"Alpaca live limit order submitted after {retry_result.attempts} attempt(s).",
            broker_order_id=str(data.get("id")) if data.get("id") else None,
            payload=data,
        )

    async def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> AlpacaLiveOrderResult:
        if not self.configured:
            return AlpacaLiveOrderResult(
                False, False, "Alpaca live keys are not configured.", None, None
            )
        if quantity <= 0:
            return AlpacaLiveOrderResult(
                True, False, "Quantity must be positive before live market submission.", None, None
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

            async def submit_once():
                response = await self.http.post(
                    f"{self.settings.alpaca_live_base_url}/v2/orders",
                    headers=self._headers(),
                    json=payload,
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                submit_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            data = response.json()
        except httpx.HTTPError as exc:
            return AlpacaLiveOrderResult(
                configured=True,
                submitted=False,
                reason=f"Alpaca live market order submission failed after bounded retries: {exc}",
                broker_order_id=None,
                payload={
                    "request": payload,
                    "max_attempts": self.settings.alpaca_order_max_attempts,
                },
            )
        return AlpacaLiveOrderResult(
            configured=True,
            submitted=True,
            reason=f"Alpaca live market order submitted after {retry_result.attempts} attempt(s).",
            broker_order_id=str(data.get("id")) if data.get("id") else None,
            payload=data,
        )

    async def cancel_all_orders(self) -> AlpacaLiveEmergencyResult:
        if not self.configured:
            return AlpacaLiveEmergencyResult(
                False, False, "Alpaca live keys are not configured.", None
            )
        try:

            async def cancel_once():
                response = await self.http.delete(
                    f"{self.settings.alpaca_live_base_url}/v2/orders",
                    headers=self._headers(),
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                cancel_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            payload = response.json() if getattr(response, "content", b"") else {}
        except httpx.HTTPError as exc:
            return AlpacaLiveEmergencyResult(
                True,
                False,
                f"Cancel-all live orders failed after bounded retries: {exc}",
                {"max_attempts": self.settings.alpaca_order_max_attempts},
            )
        return AlpacaLiveEmergencyResult(
            True,
            True,
            f"Cancel-all live orders request accepted after {retry_result.attempts} attempt(s).",
            payload,
        )

    async def cancel_order(self, broker_order_id: str) -> AlpacaLiveEmergencyResult:
        if not self.configured:
            return AlpacaLiveEmergencyResult(
                False, False, "Alpaca live keys are not configured.", None
            )
        if not broker_order_id:
            return AlpacaLiveEmergencyResult(
                True, False, "Broker order id is required for live cancellation.", None
            )
        try:

            async def cancel_once():
                response = await self.http.delete(
                    f"{self.settings.alpaca_live_base_url}/v2/orders/{broker_order_id}",
                    headers=self._headers(),
                    timeout=15,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                cancel_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            payload = response.json() if getattr(response, "content", b"") else {}
        except httpx.HTTPError as exc:
            return AlpacaLiveEmergencyResult(
                True,
                False,
                f"Alpaca live order cancel failed after bounded retries: {exc}",
                {
                    "broker_order_id": broker_order_id,
                    "max_attempts": self.settings.alpaca_order_max_attempts,
                },
            )
        return AlpacaLiveEmergencyResult(
            True,
            True,
            f"Alpaca live order cancel accepted after {retry_result.attempts} attempt(s).",
            payload,
        )

    async def flatten_all_positions(self) -> AlpacaLiveEmergencyResult:
        if not self.configured:
            return AlpacaLiveEmergencyResult(
                False, False, "Alpaca live keys are not configured.", None
            )
        try:

            async def flatten_once():
                response = await self.http.delete(
                    f"{self.settings.alpaca_live_base_url}/v2/positions",
                    headers=self._headers(),
                    params={"cancel_orders": "true"},
                    timeout=30,
                )
                response.raise_for_status()
                return response

            retry_result = await request_with_retries(
                flatten_once,
                max_attempts=self.settings.alpaca_order_max_attempts,
                backoff_seconds=self.settings.alpaca_order_retry_backoff_seconds,
            )
            response = retry_result.value
            payload = response.json() if getattr(response, "content", b"") else {}
        except httpx.HTTPError as exc:
            return AlpacaLiveEmergencyResult(
                True,
                False,
                f"Flatten-all live positions failed after bounded retries: {exc}",
                {"max_attempts": self.settings.alpaca_order_max_attempts},
            )
        return AlpacaLiveEmergencyResult(
            True,
            True,
            f"Flatten-all live positions request accepted after {retry_result.attempts} attempt(s).",
            payload,
        )

    async def _get(self, path: str, params: dict[str, str] | None = None):
        response = await self.http.get(
            f"{self.settings.alpaca_live_base_url}{path}",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_live_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_live_secret_key,
        }
