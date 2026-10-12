"""Pure, side-effect-free risk validation for live and dry-run orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Any

from .exchange import MarketRules, is_eligible_symbol, to_decimal


@dataclass(frozen=True)
class RiskLimits:
    max_order_value: Decimal = Decimal("25")
    max_total_exposure: Decimal = Decimal("100")
    max_bot_allocation: Decimal = Decimal("50")
    reserve_balance: Decimal = Decimal("10")
    daily_loss_limit: Decimal = Decimal("10")
    max_open_orders: int = 40
    max_running_bots: int = 2
    max_daily_orders: int = 200
    stale_data_seconds: int = 120
    max_consecutive_errors: int = 3
    max_price_deviation: Decimal = Decimal("0.05")

    def to_dict(self) -> dict[str, str | int]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RiskLimits":
        defaults = cls()
        values: dict[str, Any] = {}
        for item in fields(cls):
            raw = (data or {}).get(item.name, getattr(defaults, item.name))
            default = getattr(defaults, item.name)
            values[item.name] = (
                to_decimal(raw, default) if isinstance(default, Decimal) else int(raw)
            )
        return cls(**values).validated()

    def validated(self) -> "RiskLimits":
        for item in fields(self):
            value = getattr(self, item.name)
            if value <= 0 and item.name != "reserve_balance":
                raise ValueError(f"{item.name} must be greater than zero")
            if value < 0:
                raise ValueError(f"{item.name} cannot be negative")
        if self.max_price_deviation > Decimal("0.25"):
            raise ValueError("max_price_deviation cannot exceed 25%")
        if self.max_bot_allocation > self.max_total_exposure:
            raise ValueError("Bot allocation cannot exceed total exposure")
        if self.max_order_value > self.max_total_exposure:
            raise ValueError("Order value cannot exceed total exposure")
        return self


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    checks: tuple[RiskCheck, ...]

    def failures(self) -> list[str]:
        return [check.detail for check in self.checks if not check.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checks": [asdict(check) for check in self.checks],
        }


@dataclass(frozen=True)
class OrderContext:
    mode: str
    live_enabled: bool
    kill_switch: bool
    rules: MarketRules
    side: str
    price: Decimal
    quantity: Decimal
    quote_available: Decimal
    base_available: Decimal
    account_exposure: Decimal
    open_orders: int
    daily_orders: int
    daily_realized_pnl: Decimal
    market_age_seconds: float
    fee_rate: Decimal
    bot_used: Decimal | None = None
    bot_allocation: Decimal | None = None
    enforce_price_band: bool = True


def evaluate_order(context: OrderContext, limits: RiskLimits) -> RiskDecision:
    rules = context.rules
    notional = context.price * context.quantity
    is_buy = context.side == "buy"
    checks: list[RiskCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(RiskCheck(name, bool(passed), detail))

    if context.mode == "dry_run":
        add("trading_mode", True, "Dry run: no real order will be sent")
    else:
        add(
            "trading_mode",
            context.live_enabled,
            "Live trading is enabled"
            if context.live_enabled
            else "Live trading is disabled in Settings",
        )
    add(
        "kill_switch",
        not context.kill_switch,
        "Kill switch is off" if not context.kill_switch else "Kill switch is engaged",
    )
    add(
        "side",
        context.side in {"buy", "sell"},
        f"Order side is {context.side}",
    )
    eligible = is_eligible_symbol(rules.symbol)
    add(
        "eligible_market",
        eligible and rules.active,
        f"{rules.symbol} is an active COIN/USDT or COIN/USDC market"
        if eligible and rules.active
        else f"{rules.symbol} is not an eligible active COIN/USDT or COIN/USDC market",
    )
    paused = rules.buys_paused if is_buy else rules.sells_paused
    add(
        "market_side_open",
        not paused,
        f"{context.side.title()}s are open on this market"
        if not paused
        else f"NonKYC has paused {context.side}s on {rules.symbol}",
    )
    valid_numbers = (
        context.price > 0
        and context.quantity > 0
        and context.price == rules.floor_price(context.price)
        and context.quantity == rules.floor_quantity(context.quantity)
    )
    add(
        "precision",
        valid_numbers,
        f"Price uses {rules.price_decimals} decimals and quantity uses "
        f"{rules.quantity_decimals} decimals",
    )
    add(
        "min_quantity",
        context.quantity >= rules.min_quantity,
        f"Quantity {context.quantity} meets the minimum {rules.min_quantity}",
    )
    add(
        "min_notional",
        notional >= rules.min_notional,
        f"Order value {notional:.4f} {rules.quote} meets the minimum "
        f"{rules.min_notional} {rules.quote}",
    )
    add(
        "open_orders",
        context.open_orders < limits.max_open_orders,
        f"{context.open_orders} of {limits.max_open_orders} BitsBots orders are open",
    )
    add(
        "daily_orders",
        context.daily_orders < limits.max_daily_orders,
        f"{context.daily_orders} of {limits.max_daily_orders} orders submitted today",
    )
    add(
        "market_data_age",
        context.market_age_seconds <= limits.stale_data_seconds,
        f"Market data is {context.market_age_seconds:.0f}s old "
        f"(limit {limits.stale_data_seconds}s)",
    )
    if context.enforce_price_band and rules.last_price > 0:
        if is_buy:
            ceiling = rules.last_price * (1 + limits.max_price_deviation)
            add(
                "price_band",
                context.price <= ceiling,
                f"Buy price is no more than {limits.max_price_deviation * 100:.1f}% "
                f"above the last price {rules.last_price}",
            )
        else:
            floor = rules.last_price * (1 - limits.max_price_deviation)
            add(
                "price_band",
                context.price >= floor,
                f"Sell price is no more than {limits.max_price_deviation * 100:.1f}% "
                f"below the last price {rules.last_price}",
            )

    if is_buy:
        cost = notional * (1 + context.fee_rate)
        add(
            "max_order_value",
            notional <= limits.max_order_value,
            f"Order value {notional:.4f} is within the {limits.max_order_value} limit",
        )
        add(
            "reserve_balance",
            context.quote_available - cost >= limits.reserve_balance,
            f"{context.quote_available:.4f} {rules.quote} available leaves at least "
            f"{limits.reserve_balance} reserved after this order",
        )
        add(
            "total_exposure",
            context.account_exposure + notional <= limits.max_total_exposure,
            f"Exposure would be {context.account_exposure + notional:.4f} of "
            f"{limits.max_total_exposure}",
        )
        add(
            "daily_loss",
            context.daily_realized_pnl > -limits.daily_loss_limit,
            f"Today's realized P&L {context.daily_realized_pnl:.4f} is above the "
            f"-{limits.daily_loss_limit} limit",
        )
        if context.bot_allocation is not None:
            used = context.bot_used or Decimal(0)
            add(
                "bot_allocation",
                used + notional <= context.bot_allocation,
                f"Bot commitment would be {used + notional:.4f} of "
                f"{context.bot_allocation}",
            )
    else:
        add(
            "base_balance",
            context.base_available >= context.quantity,
            f"{context.base_available} {rules.base} available to sell "
            f"{context.quantity}",
        )

    return RiskDecision(all(check.passed for check in checks), tuple(checks))
