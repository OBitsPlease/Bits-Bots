"""Read-only access to a user's own NonKYC account (balances, open orders,
recent trades) using their personal API key.

Credentials are supplied once through the local Settings UI, encrypted at
rest with a key file that never leaves this machine, and are only ever used
to call NonKYC's authenticated REST endpoints directly from this process.
They are never sent anywhere else.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings


class LiveAccountError(RuntimeError):
    """Raised when a user's own NonKYC account cannot be reached or read."""


def _key_path(settings: Settings) -> Path:
    return settings.database_path.parent / "local.key"


def _load_or_create_fernet(settings: Settings) -> Fernet:
    path = _key_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key)
    return Fernet(key)


def encrypt_secret(secret: str, settings: Settings) -> str:
    return _load_or_create_fernet(settings).encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, settings: Settings) -> str:
    try:
        return _load_or_create_fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise LiveAccountError(
            "Stored NonKYC credentials could not be decrypted. Reconnect your "
            "account in Settings."
        ) from exc


def mask_access_key(access_key: str) -> str:
    if len(access_key) <= 8:
        return "•" * len(access_key)
    return f"{access_key[:4]}…{access_key[-4:]}"


class NonKycAccountClient:
    """Signed REST client for a user's own NonKYC account endpoints."""

    def __init__(self, access_key: str, secret_key: str, settings: Settings) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._base_url = settings.api_base_url
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "BitsBots/0.1"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self._secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, full_url: str) -> dict[str, str]:
        nonce = str(int(time.time() * 1000))
        signature = self._sign(self._access_key + full_url + nonce)
        return {
            "X-API-KEY": self._access_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": signature,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        query = f"?{urlencode(clean_params)}" if clean_params else ""
        # NonKYC signs the full request URL (base URL + path + query string),
        # not just the path, so the signature must match that exactly.
        full_url = f"{self._base_url}{path}{query}"
        try:
            response = await self._http.get(
                path, params=clean_params, headers=self._headers(full_url)
            )
        except httpx.HTTPError as exc:
            raise LiveAccountError(f"Unable to reach NonKYC account API: {exc}") from exc
        if response.status_code in (401, 403):
            raise LiveAccountError(
                "NonKYC rejected these API credentials. Check the key, secret, "
                "and its read permissions."
            )
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LiveAccountError(f"NonKYC account API returned an error: {exc}") from exc
        return payload

    async def get_balances(self) -> list[dict[str, Any]]:
        payload = await self._get("/balances")
        return payload if isinstance(payload, list) else []

    async def get_open_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        payload = await self._get(
            "/getorders", {"status": "active", "limit": limit, "skip": 0}
        )
        return payload if isinstance(payload, list) else []

    async def get_recent_trades(self, limit: int = 25) -> list[dict[str, Any]]:
        payload = await self._get("/gettrades", {"limit": limit, "skip": 0})
        return payload if isinstance(payload, list) else []

    async def get_all_open_orders(self, page_size: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """Pages through every active order (a single grid bot commonly has
        dozens of open orders at once, more than a single page)."""
        orders: list[dict[str, Any]] = []
        for page in range(max_pages):
            payload = await self._get(
                "/getorders", {"status": "active", "limit": page_size, "skip": page * page_size}
            )
            batch = payload if isinstance(payload, list) else []
            orders.extend(batch)
            if len(batch) < page_size:
                break
        return orders

    async def get_all_orders_any_status(
        self, page_size: int = 100, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        """Pages through active, filled AND cancelled orders. NonKYC's own
        grid bot engine tags every order it places (open or closed) with a
        ``userProvidedId`` like ``gb:<botId>:<rung>``, so this history is
        what lets BitsBots tell a real grid bot apart from manual trading."""
        orders: list[dict[str, Any]] = []
        for page in range(max_pages):
            payload = await self._get(
                "/getorders", {"status": "all", "limit": page_size, "skip": page * page_size}
            )
            batch = payload if isinstance(payload, list) else []
            orders.extend(batch)
            if len(batch) < page_size:
                break
        return orders

    async def get_all_trades(self, page_size: int = 100, max_pages: int = 10) -> list[dict[str, Any]]:
        """Pages through recent trade history so grid "fills" and booked
        profit can be reconstructed across a bot's whole lifetime, not just
        the most recent handful of trades."""
        trades: list[dict[str, Any]] = []
        for page in range(max_pages):
            payload = await self._get(
                "/gettrades", {"limit": page_size, "skip": page * page_size}
            )
            batch = payload if isinstance(payload, list) else []
            trades.extend(batch)
            if len(batch) < page_size:
                break
        return trades


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _symbol_of(item: dict[str, Any]) -> str:
    # NonKYC nests the market's symbol under a "market" object
    # (e.g. {"market": {"symbol": "BTC/USDT"}}) rather than a top-level
    # "symbol" field, for both orders and trades.
    market = item.get("market")
    if isinstance(market, dict) and market.get("symbol"):
        return str(market["symbol"])
    return str(item.get("symbol") or "")


# NonKYC's own grid bot engine stamps every order it places (open, filled,
# or cancelled) with a "userProvidedId" of the form "gb:<botId>:<rung>",
# where <botId> is stable for the lifetime of that specific bot and <rung>
# is the grid ladder step. That is the ONLY reliable way to tell a real
# grid bot apart from ordinary manual trading on the same market - an
# order/fill count heuristic can't do it (e.g. plenty of manual trading on
# a market can rack up dozens of fills without ever being a bot).
_GRID_BOT_ID_PATTERN = re.compile(r"^gb:([^:]+):(\d+)$")


def _grid_bot_tag(order: dict[str, Any]) -> tuple[str, int] | None:
    raw = order.get("userProvidedId")
    if not isinstance(raw, str):
        return None
    match = _GRID_BOT_ID_PATTERN.match(raw.strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


async def fetch_live_account_snapshot(
    access_key: str, secret_key: str, settings: Settings
) -> dict[str, Any]:
    client = NonKycAccountClient(access_key, secret_key, settings)
    try:
        balances = await client.get_balances()
        open_orders = await client.get_open_orders()
        recent_trades = await client.get_recent_trades()
    finally:
        await client.close()

    as_float = _as_float
    symbol_of = _symbol_of

    nonzero_balances = [
        {
            "asset": str(item.get("asset") or item.get("ticker") or item.get("currency") or ""),
            "available": as_float(item.get("available")),
            # NonKYC reports "held" and "pending" as separate locked-fund
            # buckets (e.g. held in an open order vs. pending settlement),
            # not alternates for the same value, so both must be summed.
            "held": as_float(item.get("held")) + as_float(item.get("pending")),
            "total": as_float(item.get("total"))
            or (
                as_float(item.get("available"))
                + as_float(item.get("held"))
                + as_float(item.get("pending"))
            ),
        }
        for item in balances
        if as_float(item.get("total"))
        or as_float(item.get("available"))
        or as_float(item.get("held"))
        or as_float(item.get("pending"))
    ]
    normalized_orders = [
        {
            "id": str(item.get("id") or item.get("orderId") or ""),
            "symbol": symbol_of(item),
            "side": str(item.get("side") or ""),
            "type": str(item.get("type") or ""),
            "price": as_float(item.get("price")),
            "quantity": as_float(item.get("quantity") or item.get("origQuantity")),
            "remaining": as_float(item.get("remainQuantity") or item.get("quantity")),
            "status": str(item.get("status") or ""),
            "created_at": item.get("createdAt") or item.get("timestamp"),
        }
        for item in open_orders
    ]
    normalized_trades = [
        {
            "id": str(item.get("id") or item.get("tradeId") or ""),
            "symbol": symbol_of(item),
            "side": str(item.get("side") or ""),
            "price": as_float(item.get("price")),
            "quantity": as_float(item.get("quantity")),
            "fee": as_float(item.get("fee")),
            "created_at": item.get("createdAt") or item.get("timestamp"),
        }
        for item in recent_trades
    ]
    return {
        "balances": nonzero_balances,
        "open_orders": normalized_orders,
        "recent_trades": normalized_trades,
    }


async def fetch_live_grid_bots(
    access_key: str, secret_key: str, settings: Settings
) -> dict[str, Any]:
    """Builds a per-bot view that mirrors NonKYC's own Grid Bots page
    (https://nonkyc.io/account/gridbots) - one entry per actual bot you
    created, not one entry per market you've ever traded on.

    NonKYC has no dedicated "list my grid bots" API endpoint, but every
    order a grid bot places (open, filled, or cancelled) is stamped with a
    ``userProvidedId`` of the form ``gb:<botId>:<rung>``. That botId is
    unique and stable for a specific bot's whole lifetime, so grouping
    orders by it - instead of by market symbol - gives real, per-bot figures
    and cleanly separates manual trading (which carries no such tag) from
    actual bot activity.

    NonKYC's ``/getorders?status=all`` endpoint does *not* keep every order a
    bot has ever placed - superseded/cancelled rungs eventually drop off, so
    reconstructing fills and P&L purely from that order list under-counts a
    bot's real activity. ``/gettrades`` retains a much longer trade history,
    so fills/booked-profit/unrealized P&L are instead reconstructed from
    trades on that bot's market whenever exactly one bot owns that market
    (true for the overwhelming common case of one bot per symbol). Active
    order state is still what drives the bot's status, grid range and
    "invested" figure, since only currently-active orders reflect the bot's
    live ladder.
    """
    from .nonkyc import NonKycClient  # local import avoids a circular import

    account_client = NonKycAccountClient(access_key, secret_key, settings)
    public_client = NonKycClient(settings)
    try:
        all_orders = await account_client.get_all_orders_any_status()
        all_trades = await account_client.get_all_trades(max_pages=30)
        try:
            markets = await public_client.get_markets()
        except Exception:  # noqa: BLE001 - price lookup is best-effort
            markets = []
    finally:
        await account_client.close()
        await public_client.close()

    as_float = _as_float
    symbol_of = _symbol_of
    last_price_by_symbol = {market.symbol: market.last_price for market in markets}

    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for trade in all_trades:
        trades_by_symbol.setdefault(symbol_of(trade), []).append(trade)

    bot_orders: dict[str, list[dict[str, Any]]] = {}
    manual_orders_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in all_orders:
        tag = _grid_bot_tag(order)
        if tag is None:
            manual_orders_by_symbol.setdefault(symbol_of(order), []).append(order)
            continue
        bot_id, _rung = tag
        bot_orders.setdefault(bot_id, []).append(order)

    # A market only maps cleanly onto one bot's trade history when exactly
    # one bot has ever traded it - that is true for the common case here.
    # When more than one bot shares a market there's no way to tell trades
    # apart, so those bots fall back to the (less complete) order-based fill
    # reconstruction instead of misattributing another bot's fills.
    symbol_bot_counts: dict[str, int] = {}
    for orders in bot_orders.values():
        symbol = next((symbol_of(o) for o in orders if symbol_of(o)), "")
        if symbol:
            symbol_bot_counts[symbol] = symbol_bot_counts.get(symbol, 0) + 1

    def _fill_side_totals(
        fills: list[dict[str, Any]], qty_field: str
    ) -> tuple[float, float, float, float]:
        buy_qty = sum(as_float(f.get(qty_field)) for f in fills if f.get("side") == "buy")
        buy_value = sum(
            as_float(f.get("price")) * as_float(f.get(qty_field))
            for f in fills
            if f.get("side") == "buy"
        )
        sell_qty = sum(as_float(f.get(qty_field)) for f in fills if f.get("side") == "sell")
        sell_value = sum(
            as_float(f.get("price")) * as_float(f.get(qty_field))
            for f in fills
            if f.get("side") == "sell"
        )
        return buy_qty, buy_value, sell_qty, sell_value

    def _pnl_stats(
        orders: list[dict[str, Any]], symbol: str, trades: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        # Fills and booked/unrealized P&L come from actual trade history when
        # this market belongs to exactly one bot - NonKYC's own order-history
        # endpoint prunes superseded grid rungs, so it under-counts fills,
        # while trade history does not. Otherwise fall back to reconstructing
        # fills from this bot's own orders (less complete, but unambiguous).
        if trades is not None:
            fills = trades
            buy_qty, buy_value, sell_qty, sell_value = _fill_side_totals(fills, "quantity")
        else:
            fills = [o for o in orders if as_float(o.get("executedQuantity")) > 0]
            buy_qty, buy_value, sell_qty, sell_value = _fill_side_totals(fills, "executedQuantity")

        avg_buy_price = (buy_value / buy_qty) if buy_qty > 0 else 0.0
        held_qty = max(0.0, buy_qty - sell_qty)
        grid_profit = (sell_value - avg_buy_price * sell_qty) if buy_qty > 0 else sell_value

        current_price = last_price_by_symbol.get(symbol)
        if not current_price:
            active_prices = [as_float(o.get("price")) for o in orders if o.get("isActive")]
            current_price = active_prices[0] if active_prices else None
        if not current_price and orders:
            current_price = as_float(orders[0].get("price"))
        if not current_price:
            current_price = avg_buy_price
        current_price = as_float(current_price)
        unrealized = held_qty * (current_price - avg_buy_price) if avg_buy_price else 0.0
        total_pnl = grid_profit + unrealized

        # "Invested" tracks NonKYC's own figure closely as the notional value
        # (price x original order size) of every currently-active rung on
        # this bot's ladder, buy or sell - both represent capital the bot
        # still has deployed into this market right now.
        active_orders = [o for o in orders if o.get("isActive")]
        active_prices_all = [as_float(o.get("price")) for o in active_orders if as_float(o.get("price"))]
        invested = sum(
            as_float(o.get("price")) * as_float(o.get("quantity"))
            for o in active_orders
        )

        timestamps = [o.get("createdAt") for o in orders if o.get("createdAt")]
        updates = [o.get("updatedAt") for o in orders if o.get("updatedAt")]

        return {
            "status": "RUNNING" if active_orders else "STOPPED",
            "grids": len({tag[1] for o in orders if (tag := _grid_bot_tag(o)) is not None})
            or len(orders),
            "active_orders": len(active_orders),
            "range_low": min(active_prices_all) if active_prices_all else None,
            "range_high": max(active_prices_all) if active_prices_all else None,
            "invested": invested,
            "fills": len(fills),
            "grid_profit": grid_profit,
            "unrealized": unrealized,
            "total_pnl": total_pnl,
            "current_price": current_price,
            "first_seen": min(timestamps) if timestamps else None,
            "last_activity": max(updates) if updates else None,
            "fills_are_estimated": trades is None,
        }

    bots: list[dict[str, Any]] = []
    for bot_id, orders in bot_orders.items():
        symbol = next((symbol_of(o) for o in orders if symbol_of(o)), "")
        if not symbol:
            continue
        trades = None
        if symbol_bot_counts.get(symbol) == 1:
            symbol_trades = trades_by_symbol.get(symbol, [])
            # NonKYC's own order history has already pruned superseded rungs
            # by the time we can see it, so the *oldest order still visible*
            # is the best cutoff we have for "since this bot's current
            # ladder began" - trades from before that point belong to an
            # earlier configuration/incarnation of the bot (or predate it
            # entirely) and would otherwise badly skew its average-cost P&L.
            earliest_order_at = min(
                (o.get("createdAt") for o in orders if o.get("createdAt")), default=None
            )
            trades = [
                t
                for t in symbol_trades
                if earliest_order_at is None
                or (t.get("createdAt") or t.get("timestamp") or 0) >= earliest_order_at
            ]
        stats = _pnl_stats(orders, symbol, trades)
        bots.append(
            {
                "bot_id": bot_id,
                "short_id": bot_id[:8],
                "symbol": symbol,
                **stats,
            }
        )

    other_activity: list[dict[str, Any]] = []
    for symbol, orders in manual_orders_by_symbol.items():
        if not symbol:
            continue
        trades = trades_by_symbol.get(symbol) if symbol not in symbol_bot_counts else None
        stats = _pnl_stats(orders, symbol, trades)
        other_activity.append({"symbol": symbol, **stats})

    bots.sort(key=lambda bot: bot["total_pnl"], reverse=True)
    other_activity.sort(key=lambda bot: bot["total_pnl"], reverse=True)
    running = [bot for bot in bots if bot["status"] == "RUNNING"]
    total_pnl_all = sum(bot["total_pnl"] for bot in bots)
    grid_profit_all = sum(bot["grid_profit"] for bot in bots)
    unrealized_all = sum(bot["unrealized"] for bot in bots)
    invested_all = sum(bot["invested"] for bot in bots)

    return {
        "bots": bots,
        "other_activity": other_activity,
        "bots_running": len(running),
        "bots_idle": len(bots) - len(running),
        "markets_traded": len(bots),
        "total_pnl": total_pnl_all,
        "grid_profit_booked": grid_profit_all,
        "unrealized": unrealized_all,
        "invested": invested_all,
        "best_performer": bots[0] if bots else None,
        "worst_performer": bots[-1] if bots and len(bots) > 1 else None,
    }

