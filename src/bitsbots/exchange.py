"""Exchange connector contract shared by live and dry-run execution.

Every value that represents money, price, or quantity is a ``Decimal``.
Connectors raise the error classes below so the execution layer can tell a
definitive rejection (no order exists) apart from an ambiguous outcome (an
order may exist and must be reconciled before anything else happens).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

QUOTE_ASSETS = ("USDT", "USDC")
STABLE_BASES = frozenset({"USDT", "USDC"})


class ExchangeError(RuntimeError):
    """Base class for connector failures."""


class ExchangeRejected(ExchangeError):
    """The exchange definitively did not create or change the order."""


class ExchangeAmbiguous(ExchangeError):
    """The request may have been applied. Reconcile before acting again."""


class ExchangeAuthError(ExchangeRejected):
    """Credentials, permissions, or IP allowlisting rejected the request."""


class ExchangeRateLimited(ExchangeRejected):
    """The exchange throttled the request before processing it."""


class ExchangeUnavailable(ExchangeRejected):
    """The request never reached the exchange (connection not established)."""


class ExchangeNotFound(ExchangeError):
    """The requested order is not known to the exchange."""


@dataclass(frozen=True)
class MarketRules:
    symbol: str
    base: str
    quote: str
    price_decimals: int
    quantity_decimals: int
    min_quantity: Decimal
    min_notional: Decimal
    last_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    active: bool
    buys_paused: bool
    sells_paused: bool
    fetched_at: int

    @property
    def price_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.price_decimals)

    @property
    def quantity_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.quantity_decimals)

    def floor_price(self, value: Decimal) -> Decimal:
        return value.quantize(self.price_step, rounding=ROUND_DOWN)

    def floor_quantity(self, value: Decimal) -> Decimal:
        return value.quantize(self.quantity_step, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    client_id: str
    order_type: str = "limit"


@dataclass(frozen=True)
class ExchangeOrder:
    exchange_id: str
    client_id: str | None
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    executed_quantity: Decimal
    status: str
    created_at: int
    updated_at: int
    raw_status: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "cancelled"}


@dataclass(frozen=True)
class Balance:
    asset: str
    available: Decimal
    held: Decimal


@dataclass(frozen=True)
class Capabilities:
    exchange: str
    spot_limit_orders: bool = True
    market_orders: bool = False
    client_order_ids: bool = True
    client_id_max_length: int = 31
    client_id_unique_enforced: bool = False
    native_grid_bots: bool = False
    native_trailing_up: bool = False
    testnet: bool = False
    ip_allowlisting: bool = True
    api_allowlist_management: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


class ExchangeConnector(Protocol):
    capabilities: Capabilities

    async def market_rules(self, symbol: str) -> MarketRules: ...

    async def balances(self) -> dict[str, Balance]: ...

    async def place_order(self, request: OrderRequest) -> ExchangeOrder: ...

    async def get_order(self, order_id: str) -> ExchangeOrder: ...

    async def list_orders(
        self, symbol: str | None = None, since: int | None = None, limit: int = 100
    ) -> list[ExchangeOrder]: ...

    async def cancel_order(self, exchange_id: str) -> None: ...

    async def close(self) -> None: ...


def split_symbol(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        raise ValueError(f"Invalid market symbol {symbol!r}")
    base, quote = symbol.strip().upper().split("/", maxsplit=1)
    return base, quote


def is_eligible_symbol(symbol: str) -> bool:
    """Only COIN/USDT and COIN/USDC markets may be traded."""
    try:
        base, quote = split_symbol(symbol)
    except ValueError:
        return False
    return bool(base) and quote in QUOTE_ASSETS and base not in STABLE_BASES


def to_decimal(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value is None or value == "":
        return default
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return default
    return result if result.is_finite() else default


def normalize_order_status(
    raw_status: str, is_active: bool, quantity: Decimal, executed: Decimal
) -> str:
    """Maps exchange status strings to: open, partially_filled, filled, cancelled."""
    if is_active:
        return "partially_filled" if executed > 0 else "open"
    if quantity > 0 and executed >= quantity:
        return "filled"
    if raw_status.strip().lower() == "filled":
        return "filled"
    return "cancelled"
