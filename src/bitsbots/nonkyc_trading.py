"""Signed NonKYC REST connector for spot order execution.

Implements only operations documented in NonKYC REST API v1.3.0
(https://api.nonkyc.io/openapi.json). NonKYC support confirmed on
2026-10-01 that there is no public native grid-bot API or testnet, that
client order IDs must be under 32 characters, and that their uniqueness is
not enforced by the exchange, so BitsBots enforces uniqueness locally.

``/cancelallorders`` is deliberately not used: it would also cancel orders
owned by the user's native NonKYC grid bots.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .config import Settings
from .exchange import (
    Balance,
    Capabilities,
    ExchangeAmbiguous,
    ExchangeAuthError,
    ExchangeError,
    ExchangeNotFound,
    ExchangeOrder,
    ExchangeRateLimited,
    ExchangeRejected,
    ExchangeUnavailable,
    MarketRules,
    OrderRequest,
    normalize_order_status,
    split_symbol,
    to_decimal,
)

NONKYC_CAPABILITIES = Capabilities(
    exchange="nonkyc",
    spot_limit_orders=True,
    market_orders=False,
    client_order_ids=True,
    client_id_max_length=31,
    client_id_unique_enforced=False,
    native_grid_bots=False,
    native_trailing_up=False,
    testnet=False,
    ip_allowlisting=True,
    api_allowlist_management=False,
    notes=(
        "No public native grid-bot or Trailing Up API; BitsBots runs grids locally.",
        "No sandbox or testnet; use BitsBots dry-run mode for rehearsal.",
        "Rate limits are not published; BitsBots spaces writes conservatively.",
        "Client order IDs must be under 32 characters and are not unique-enforced.",
    ),
)

ORDER_NOT_FOUND_CODES = {20002}
WRITE_INTERVAL_SECONDS = 1.0
RATE_LIMIT_BACKOFF_SECONDS = 30.0


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _error_details(response: httpx.Response) -> tuple[int | None, str]:
    try:
        payload = response.json()
    except ValueError:
        return None, response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        message = " ".join(
            str(part)
            for part in (error.get("message"), error.get("description"))
            if part
        )
        return (int(code) if isinstance(code, int) else None), message
    return None, str(payload)[:200]


def parse_order(item: dict[str, Any]) -> ExchangeOrder:
    market = item.get("market")
    symbol = (
        str(market.get("symbol"))
        if isinstance(market, dict) and market.get("symbol")
        else str(item.get("symbol") or "")
    )
    quantity = to_decimal(item.get("quantity"))
    executed = to_decimal(item.get("executedQuantity"))
    raw_status = str(item.get("status") or "")
    is_active = bool(item.get("isActive", raw_status.lower() in {"active", "new"}))
    created_at = int(to_decimal(item.get("createdAt")))
    return ExchangeOrder(
        exchange_id=str(item.get("id") or ""),
        client_id=(str(item["userProvidedId"]) if item.get("userProvidedId") else None),
        symbol=symbol,
        side=str(item.get("side") or "").lower(),
        price=to_decimal(item.get("price")),
        quantity=quantity,
        executed_quantity=executed,
        status=normalize_order_status(raw_status, is_active, quantity, executed),
        created_at=created_at,
        updated_at=int(to_decimal(item.get("updatedAt"), Decimal(created_at))),
        raw_status=raw_status,
    )


def parse_market_rules(item: dict[str, Any], symbol: str) -> MarketRules:
    base, quote_asset = split_symbol(str(item.get("symbol") or symbol))
    min_notional = (
        to_decimal(item.get("minQuote")) if item.get("isMinQuoteActive") else Decimal(0)
    )
    best_bid = to_decimal(item.get("bestBid"))
    best_ask = to_decimal(item.get("bestAsk"))
    return MarketRules(
        symbol=f"{base}/{quote_asset}",
        base=base,
        quote=quote_asset,
        price_decimals=int(item.get("priceDecimals", 8)),
        quantity_decimals=int(item.get("quantityDecimals", 8)),
        min_quantity=to_decimal(item.get("minimumQuantity")),
        min_notional=min_notional,
        last_price=to_decimal(item.get("lastPrice")),
        best_bid=best_bid if best_bid > 0 else None,
        best_ask=best_ask if best_ask > 0 else None,
        active=bool(item.get("isActive"))
        and not bool(item.get("isPaused"))
        and not bool(item.get("apiExcluded")),
        buys_paused=bool(item.get("pauseBuys")),
        sells_paused=bool(item.get("pauseSells")),
        fetched_at=int(time.time() * 1000),
    )


class NonKycPublicRules:
    """Unauthenticated market-rule reader with a short cache, used by dry-run
    execution and by previews that do not need account access."""

    def __init__(
        self,
        settings: Settings,
        cache_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.api_base_url.rstrip("/"),
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "BitsBots/0.1"},
            transport=transport,
        )
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, MarketRules]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def __call__(self, symbol: str) -> MarketRules:
        key = symbol.strip().upper()
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        try:
            response = await self._http.get("/market/info", params={"symbol": key})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExchangeError(f"Unable to read NonKYC market rules for {key}: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("symbol"):
            raise ExchangeError(f"NonKYC has no market named {key}")
        rules = parse_market_rules(payload, key)
        self._cache[key] = (time.monotonic(), rules)
        return rules


class NonKycTradingClient:
    capabilities = NONKYC_CAPABILITIES

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._base_url = settings.api_base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers={"User-Agent": "BitsBots/0.1"},
            transport=transport,
        )
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0
        self._backoff_until = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self._secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _headers(self, full_url: str, body: str = "") -> dict[str, str]:
        nonce = str(int(time.time() * 1000))
        return {
            "X-API-KEY": self._access_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": self._sign(self._access_key + full_url + body + nonce),
            "Content-Type": "application/json",
        }

    def _check_backoff(self) -> None:
        if time.monotonic() < self._backoff_until:
            raise ExchangeRateLimited(
                "NonKYC recently rate limited this account; waiting before retrying"
            )

    def _raise_for_status(self, response: httpx.Response, ambiguous_5xx: bool) -> None:
        if response.status_code < 400:
            return
        code, message = _error_details(response)
        if response.status_code in (401, 403) or code in {1001, 1002, 1003, 1004, 403}:
            raise ExchangeAuthError(
                "NonKYC rejected the API credentials, permissions, or IP "
                f"allowlist ({message or response.status_code})"
            )
        if response.status_code == 429 or code == 429:
            self._backoff_until = time.monotonic() + RATE_LIMIT_BACKOFF_SECONDS
            raise ExchangeRateLimited("NonKYC rate limited the request")
        if code in ORDER_NOT_FOUND_CODES or response.status_code == 404:
            raise ExchangeNotFound(message or "Order not found")
        if response.status_code >= 500:
            error_class = ExchangeAmbiguous if ambiguous_5xx else ExchangeError
            raise error_class(
                f"NonKYC returned HTTP {response.status_code}: {message}".strip()
            )
        raise ExchangeRejected(message or f"NonKYC rejected the request ({response.status_code})")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        query = f"?{urlencode(clean)}" if clean else ""
        full_url = f"{self._base_url}{path}{query}"
        try:
            response = await self._http.get(
                path, params=clean, headers=self._headers(full_url)
            )
        except httpx.HTTPError as exc:
            raise ExchangeError(f"Unable to reach NonKYC: {exc}") from exc
        self._raise_for_status(response, ambiguous_5xx=False)
        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeError("NonKYC returned an unreadable response") from exc

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        """Signed write. Any outcome that may have reached the matching engine
        is reported as ExchangeAmbiguous so the caller reconciles instead of
        retrying."""
        self._check_backoff()
        async with self._write_lock:
            wait = WRITE_INTERVAL_SECONDS - (time.monotonic() - self._last_write)
            if wait > 0:
                await asyncio.sleep(wait)
            serialized = json.dumps(body, separators=(",", ":"), sort_keys=True)
            full_url = f"{self._base_url}{path}"
            self._last_write = time.monotonic()
            try:
                response = await self._http.post(
                    path,
                    content=serialized.encode("utf-8"),
                    headers=self._headers(full_url, serialized),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ExchangeUnavailable(
                    f"Could not connect to NonKYC; the request was not sent ({exc})"
                ) from exc
            except httpx.HTTPError as exc:
                raise ExchangeAmbiguous(
                    f"Connection to NonKYC failed after sending the request ({exc})"
                ) from exc
        self._raise_for_status(response, ambiguous_5xx=True)
        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeAmbiguous(
                "NonKYC accepted the request but returned an unreadable response"
            ) from exc

    async def market_rules(self, symbol: str) -> MarketRules:
        payload = await self._get("/market/info", {"symbol": symbol})
        if not isinstance(payload, dict) or not payload.get("symbol"):
            raise ExchangeError(f"NonKYC returned no market information for {symbol}")
        return parse_market_rules(payload, symbol)

    async def balances(self) -> dict[str, Balance]:
        payload = await self._get("/balances")
        result: dict[str, Balance] = {}
        for item in payload if isinstance(payload, list) else []:
            asset = str(item.get("asset") or item.get("ticker") or "").upper()
            if not asset:
                continue
            result[asset] = Balance(
                asset=asset,
                available=to_decimal(item.get("available")),
                held=to_decimal(item.get("held")) + to_decimal(item.get("pending")),
            )
        return result

    async def place_order(self, request: OrderRequest) -> ExchangeOrder:
        if request.order_type != "limit":
            raise ExchangeRejected("BitsBots submits limit orders only")
        if len(request.client_id) > self.capabilities.client_id_max_length:
            raise ExchangeRejected("Client order ID exceeds NonKYC's length limit")
        payload = await self._post(
            "/createorder",
            {
                "userProvidedId": request.client_id,
                "symbol": request.symbol,
                "side": request.side,
                "type": "limit",
                "quantity": _format_decimal(request.quantity),
                "price": _format_decimal(request.price),
                "strictValidate": True,
            },
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ExchangeAmbiguous("NonKYC did not return an order ID")
        return parse_order(payload)

    async def get_order(self, order_id: str) -> ExchangeOrder:
        payload = await self._get(f"/getorder/{quote(order_id, safe='')}")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ExchangeNotFound(f"Order {order_id} was not found")
        return parse_order(payload)

    async def list_orders(
        self, symbol: str | None = None, since: int | None = None, limit: int = 100
    ) -> list[ExchangeOrder]:
        payload = await self._get(
            "/account/orders",
            {"symbol": symbol, "since": since, "limit": min(limit, 500), "skip": 0},
        )
        return [
            parse_order(item)
            for item in (payload if isinstance(payload, list) else [])
            if isinstance(item, dict) and item.get("id")
        ]

    async def cancel_order(self, exchange_id: str) -> None:
        payload = await self._post("/cancelorder", {"id": exchange_id})
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ExchangeRejected("NonKYC refused to cancel the order")
