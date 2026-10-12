"""Dry-run exchange: rehearses the full live order lifecycle without sending
anything to an exchange.

Market rules and prices come from NonKYC's public market data, balances and
orders are simulated and persisted in the local database so restarts are
realistic. Fault injection lets tests and rehearsals exercise rejections,
timeouts, ambiguous acceptances, and partial fills.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from .exchange import (
    Balance,
    Capabilities,
    ExchangeAmbiguous,
    ExchangeNotFound,
    ExchangeOrder,
    ExchangeRejected,
    ExchangeUnavailable,
    MarketRules,
    OrderRequest,
    split_symbol,
    to_decimal,
)
from .live_store import LiveStore, now_ms

DRY_RUN_CAPABILITIES = Capabilities(
    exchange="dry_run",
    spot_limit_orders=True,
    market_orders=False,
    client_order_ids=True,
    client_id_max_length=31,
    client_id_unique_enforced=False,
    testnet=True,
    ip_allowlisting=False,
    notes=("Simulated locally with real NonKYC prices; no orders leave this PC.",),
)
DRY_RUN_STARTING_BALANCES = {"USDT": Decimal("1000"), "USDC": Decimal("1000")}

RulesSource = Callable[[str], Awaitable[MarketRules]]


@dataclass
class DryRunFaults:
    """One-shot fault switches, consumed by the next matching request."""

    reject_next: bool = False
    unavailable_next: bool = False
    ambiguous_accept_next: bool = False
    ambiguous_drop_next: bool = False
    partial_fill_ratio: Decimal | None = None


class DryRunExchange:
    capabilities = DRY_RUN_CAPABILITIES

    def __init__(
        self,
        store: LiveStore,
        user_id: int,
        rules_source: RulesSource,
        fee_rate: Decimal,
        faults: DryRunFaults | None = None,
    ) -> None:
        self.store = store
        self.user_id = user_id
        self.rules_source = rules_source
        self.fee_rate = fee_rate
        self.faults = faults or DryRunFaults()
        if not store.dry_run_balances(user_id):
            for asset, amount in DRY_RUN_STARTING_BALANCES.items():
                store.set_dry_run_balance(user_id, asset, amount, Decimal(0))

    async def close(self) -> None:
        return None

    async def market_rules(self, symbol: str) -> MarketRules:
        return await self.rules_source(symbol)

    async def balances(self) -> dict[str, Balance]:
        return {
            row["asset"]: Balance(
                asset=row["asset"],
                available=to_decimal(row["available"]),
                held=to_decimal(row["held"]),
            )
            for row in self.store.dry_run_balances(self.user_id)
        }

    def _adjust(self, asset: str, available: Decimal, held: Decimal) -> None:
        current_available, current_held = self.store.dry_run_balance(self.user_id, asset)
        new_available = current_available + available
        new_held = current_held + held
        if new_available < 0 or new_held < 0:
            raise ExchangeRejected(f"Insufficient dry-run {asset} balance")
        self.store.set_dry_run_balance(self.user_id, asset, new_available, new_held)

    async def place_order(self, request: OrderRequest) -> ExchangeOrder:
        if self.faults.unavailable_next:
            self.faults.unavailable_next = False
            raise ExchangeUnavailable("Simulated connection failure before sending")
        if self.faults.reject_next:
            self.faults.reject_next = False
            raise ExchangeRejected("Simulated exchange rejection")
        if self.faults.ambiguous_drop_next:
            self.faults.ambiguous_drop_next = False
            raise ExchangeAmbiguous("Simulated timeout; the order was not created")
        base, quote = split_symbol(request.symbol)
        if request.side == "buy":
            reserve = request.price * request.quantity * (1 + self.fee_rate)
            self._adjust(quote, -reserve, reserve)
        else:
            self._adjust(base, -request.quantity, request.quantity)
        timestamp = now_ms()
        order = {
            "id": f"dry-{secrets.token_hex(6)}",
            "user_id": self.user_id,
            "client_id": request.client_id,
            "symbol": request.symbol,
            "side": request.side,
            "price": str(request.price),
            "quantity": str(request.quantity),
            "executed": "0",
            "status": "open",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.store.save_dry_run_order(order)
        if self.faults.ambiguous_accept_next:
            self.faults.ambiguous_accept_next = False
            raise ExchangeAmbiguous("Simulated timeout after the order was accepted")
        return await self._simulate(order)

    async def _simulate(self, order: dict) -> ExchangeOrder:
        """Fills resting orders when the real market price crosses them."""
        if order["status"] in {"open", "partially_filled"}:
            try:
                rules = await self.rules_source(order["symbol"])
            except Exception:  # noqa: BLE001 - price unavailable, leave order resting
                rules = None
            price = to_decimal(order["price"])
            if rules is not None and rules.last_price > 0:
                crossed = (
                    rules.last_price <= price
                    if order["side"] == "buy"
                    else rules.last_price >= price
                )
                if crossed:
                    self._fill(order)
        return self._to_exchange_order(order)

    def _fill(self, order: dict) -> None:
        quantity = to_decimal(order["quantity"])
        executed = to_decimal(order["executed"])
        remaining = quantity - executed
        fill = remaining
        ratio = self.faults.partial_fill_ratio
        if ratio is not None and executed == 0:
            self.faults.partial_fill_ratio = None
            fill = (remaining * ratio).quantize(Decimal("0.00000001"))
        if fill <= 0:
            return
        price = to_decimal(order["price"])
        base, quote = split_symbol(order["symbol"])
        if order["side"] == "buy":
            reserved = price * fill * (1 + self.fee_rate)
            self._adjust(quote, Decimal(0), -reserved)
            self._adjust(base, fill, Decimal(0))
        else:
            self._adjust(base, Decimal(0), -fill)
            self._adjust(quote, price * fill * (1 - self.fee_rate), Decimal(0))
        executed += fill
        order["executed"] = str(executed)
        order["status"] = "filled" if executed >= quantity else "partially_filled"
        order["updated_at"] = now_ms()
        self.store.save_dry_run_order(order)

    def _to_exchange_order(self, order: dict) -> ExchangeOrder:
        return ExchangeOrder(
            exchange_id=order["id"],
            client_id=order["client_id"],
            symbol=order["symbol"],
            side=order["side"],
            price=to_decimal(order["price"]),
            quantity=to_decimal(order["quantity"]),
            executed_quantity=to_decimal(order["executed"]),
            status=order["status"],
            created_at=int(order["created_at"]),
            updated_at=int(order["updated_at"]),
            raw_status=order["status"],
        )

    async def get_order(self, order_id: str) -> ExchangeOrder:
        order = self.store.dry_run_order(self.user_id, order_id)
        if order is None:
            raise ExchangeNotFound(f"Dry-run order {order_id} was not found")
        return await self._simulate(order)

    async def list_orders(
        self, symbol: str | None = None, since: int | None = None, limit: int = 100
    ) -> list[ExchangeOrder]:
        orders = [
            order
            for order in self.store.dry_run_orders(self.user_id, symbol)
            if since is None or int(order["created_at"]) >= since
        ]
        return [self._to_exchange_order(order) for order in orders[:limit]]

    async def cancel_order(self, exchange_id: str) -> None:
        order = self.store.dry_run_order(self.user_id, exchange_id)
        if order is None:
            raise ExchangeNotFound(f"Dry-run order {exchange_id} was not found")
        if order["status"] not in {"open", "partially_filled"}:
            raise ExchangeNotFound("Dry-run order is no longer active")
        remaining = to_decimal(order["quantity"]) - to_decimal(order["executed"])
        base, quote = split_symbol(order["symbol"])
        if order["side"] == "buy":
            release = to_decimal(order["price"]) * remaining * (1 + self.fee_rate)
            self._adjust(quote, release, -release)
        else:
            self._adjust(base, remaining, -remaining)
        order["status"] = "cancelled"
        order["updated_at"] = now_ms()
        self.store.save_dry_run_order(order)
