"""Locally managed fixed-grid bots with optional conservative Trailing Up.

NonKYC has no public native grid-bot API, so BitsBots maintains the ladder
itself using documented spot limit orders. The engine runs only while
BitsBots is running on this PC.

Each slot buys at one grid line and sells one line higher. A slot moves
idle -> buy_open -> holding -> sell_open -> idle. Fills are applied exactly
once from reconciled order intents, and new orders are only placed when
every order the bot owns has been verified with the exchange recently.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from .exchange import ExchangeError, MarketRules, is_eligible_symbol, to_decimal
from .execution import ACTIVE_STATES_FOR_BOT, LiveTradingService, TradingError
from .live_store import ACTIVE_INTENT_STATES, now_ms
from .risk import evaluate_order

RUNNING_STATES = ("running", "recentering", "stopping")
TICK_STATES = ("running", "recentering", "stopping", "paused", "halted")
WRITES_PER_TICK = 3
RECONCILE_FRESHNESS_MS = 120_000
LIQUIDATION_DISCOUNT = Decimal("0.01")
MIN_LEVELS, MAX_LEVELS = 3, 50
SUGGEST_LOOKBACK_HOURS = 72
SUGGEST_MAX_LEVELS = 20


def _plain(value: Decimal) -> str:
    """Formats a decimal without scientific notation (e.g. 0.00000123)."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _dec(value: Any) -> Decimal:
    return to_decimal(value)


def _number(values: dict[str, Any], key: str, positive: bool = True) -> Decimal:
    try:
        value = Decimal(str(values[key]).strip())
    except Exception as exc:  # noqa: BLE001 - surfaced as validation error
        raise TradingError(f"{key.replace('_', ' ').title()} must be a number") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise TradingError(f"{key.replace('_', ' ').title()} must be greater than zero")
    return value


class GridEngine:
    def __init__(self, live: LiveTradingService) -> None:
        self.live = live
        self.store = live.store

    # ------------------------------------------------------------ planning
    def ladder(
        self, rules: MarketRules, lower: Decimal, upper: Decimal, levels: int
    ) -> list[tuple[Decimal, Decimal]]:
        step = (upper - lower) / (levels - 1)
        lines = [rules.floor_price(lower + step * index) for index in range(levels)]
        return [(lines[index], lines[index + 1]) for index in range(levels - 1)]

    async def plan(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        mode = values.get("mode", "dry_run")
        if mode not in ("dry_run", "live"):
            raise TradingError("Mode must be dry_run or live")
        symbol = str(values.get("symbol", "")).strip().upper()
        if not is_eligible_symbol(symbol):
            raise TradingError("Only COIN/USDT and COIN/USDC markets can be traded")
        name = str(values.get("name") or f"{symbol.split('/')[0]} Grid").strip()[:60]
        lower = _number(values, "lower_price")
        upper = _number(values, "upper_price")
        stop = _number(values, "stop_price")
        order_value = _number(values, "order_value")
        max_allocation = _number(values, "max_allocation")
        try:
            levels = int(values["levels"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TradingError("Grid lines must be a whole number") from exc
        trailing = bool(values.get("trailing_enabled", False))
        try:
            rules = await self.live.connector(user_id, mode).market_rules(symbol)
        except ExchangeError as exc:
            raise TradingError(str(exc)) from exc
        limits = self.store.settings(user_id)["limits"]
        fee = self.live.fee_rate
        errors: list[str] = []
        if not rules.active:
            errors.append(f"{symbol} is not currently tradable")
        if not MIN_LEVELS <= levels <= MAX_LEVELS:
            errors.append(f"Grid lines must be between {MIN_LEVELS} and {MAX_LEVELS}")
        if not stop < lower < rules.last_price < upper:
            errors.append(
                f"Use stop < lower < current price ({rules.last_price}) < upper"
            )
        if order_value > limits.max_order_value:
            errors.append(f"Order value exceeds the {limits.max_order_value} per-order limit")
        if order_value < rules.min_notional:
            errors.append(f"Order value is below NonKYC's minimum {rules.min_notional}")
        if max_allocation > limits.max_bot_allocation:
            errors.append(f"Allocation exceeds the {limits.max_bot_allocation} bot limit")
        if order_value > max_allocation:
            errors.append("Order value cannot exceed the bot allocation")
        trailing_values: dict[str, Any] = {
            "trailing_enabled": int(trailing),
            "trailing_trigger": "0.01",
            "trailing_confirm_seconds": 300,
            "trailing_max_recenters": 3,
            "trailing_ceiling": None,
        }
        if trailing:
            trigger = _number(values, "trailing_trigger")
            ceiling = _number(values, "trailing_ceiling")
            try:
                confirm = int(values.get("trailing_confirm_seconds", 300))
                max_recenters = int(values.get("trailing_max_recenters", 3))
            except (TypeError, ValueError) as exc:
                raise TradingError("Trailing timing values must be whole numbers") from exc
            if not Decimal("0.001") <= trigger <= Decimal("0.2"):
                errors.append("Trailing trigger must be between 0.1% and 20%")
            if not 60 <= confirm <= 86_400:
                errors.append("Trailing confirmation must be 60 seconds to 24 hours")
            if not 1 <= max_recenters <= 20:
                errors.append("Maximum recenters must be 1 to 20")
            if ceiling <= upper:
                errors.append("Trailing ceiling must be above the upper price")
            trailing_values.update(
                trailing_trigger=str(trigger),
                trailing_confirm_seconds=confirm,
                trailing_max_recenters=max_recenters,
                trailing_ceiling=str(rules.floor_price(ceiling)),
            )
        slots = self.ladder(rules, lower, upper, levels) if MIN_LEVELS <= levels <= MAX_LEVELS else []
        rows = []
        initial_commitment = Decimal(0)
        for buy, sell in slots:
            quantity = rules.floor_quantity(order_value / buy) if buy > 0 else Decimal(0)
            notional = buy * quantity
            cycle_profit = quantity * sell * (1 - fee) - notional * (1 + fee)
            places_now = buy < rules.last_price
            if places_now and initial_commitment + notional <= max_allocation:
                initial_commitment += notional
            rows.append(
                {
                    "buy_price": str(buy),
                    "sell_price": str(sell),
                    "quantity": str(quantity),
                    "notional": str(notional),
                    "cycle_profit": str(cycle_profit),
                    "places_now": places_now,
                }
            )
        if slots:
            spacing = min(sell / buy - 1 for buy, sell in slots if buy > 0)
            if spacing <= 2 * fee:
                errors.append(
                    f"Grid spacing {spacing * 100:.3f}% does not cover round-trip fees "
                    f"({2 * fee * 100:.2f}%). Use fewer lines or a wider range."
                )
            if any(Decimal(row["quantity"]) <= rules.min_quantity or Decimal(row["quantity"]) <= 0 for row in rows):
                errors.append("Order value is too small for at least one grid line")
            if any(Decimal(row["notional"]) < rules.min_notional for row in rows):
                errors.append("At least one grid order is below NonKYC's minimum order value")
        warnings = [
            "BitsBots runs this grid on this PC. It only trades while BitsBots is running.",
            "The grid starts by placing buys below the current price; it does not buy up front.",
        ]
        if mode == "live":
            warnings.append("LIVE: this bot will place real orders with real funds on NonKYC")
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "mode": mode,
            "symbol": symbol,
            "last_price": str(rules.last_price),
            "slots": rows,
            "initial_commitment": str(initial_commitment),
            "bot": {
                "mode": mode,
                "name": name,
                "symbol": symbol,
                "lower_price": str(rules.floor_price(lower)),
                "upper_price": str(rules.floor_price(upper)),
                "levels": levels,
                "order_value": str(order_value),
                "max_allocation": str(max_allocation),
                "stop_price": str(rules.floor_price(stop)),
                **trailing_values,
            },
        }

    async def suggest(
        self,
        user_id: int,
        mode: str,
        symbol: str,
        recent_ranges: list[tuple[Decimal, Decimal]],
    ) -> dict[str, Any]:
        """Suggests grid settings from recent prices, market rules, balances,
        and the user's risk limits, then validates them with plan()."""
        if mode not in ("dry_run", "live"):
            raise TradingError("Mode must be dry_run or live")
        symbol = symbol.strip().upper()
        if not is_eligible_symbol(symbol):
            raise TradingError("Only COIN/USDT and COIN/USDC markets can be traded")
        connector = self.live.connector(user_id, mode)
        try:
            rules = await connector.market_rules(symbol)
        except ExchangeError as exc:
            raise TradingError(str(exc)) from exc
        price = rules.last_price
        if price <= 0:
            raise TradingError(f"{symbol} has no recent price to base suggestions on")
        limits = self.store.settings(user_id)["limits"]
        fee = self.live.fee_rate
        notes: list[str] = []

        recent = recent_ranges[-SUGGEST_LOOKBACK_HOURS:]
        if len(recent) >= 12:
            high = max(item[0] for item in recent)
            low = min(item[1] for item in recent)
            notes.append(
                f"Over the last {len(recent)} hours {symbol} traded between "
                f"{_plain(rules.floor_price(low))} and {_plain(rules.floor_price(high))}; "
                f"it is now {_plain(price)}."
            )
        else:
            high, low = price * Decimal("1.08"), price * Decimal("0.92")
            notes.append("Not enough recent price history, so the range is set to about ±8% around the current price.")
        lower = max(min(low, price * Decimal("0.97")), price * Decimal("0.75"))
        upper = min(max(high, price * Decimal("1.03")), price * Decimal("1.25"))
        lower, upper = rules.floor_price(lower), rules.floor_price(upper)
        if not lower < price < upper:
            raise TradingError(f"{symbol}'s price precision is too coarse for a grid")

        target_spacing = max(fee * 6, Decimal("0.01"))
        levels = MIN_LEVELS
        for candidate in range(SUGGEST_MAX_LEVELS, MIN_LEVELS - 1, -1):
            ladder = self.ladder(rules, lower, upper, candidate)
            if all(buy > 0 for buy, _ in ladder) and min(sell / buy - 1 for buy, sell in ladder) >= target_spacing:
                levels = candidate
                break

        budget = limits.max_bot_allocation
        headroom = limits.max_total_exposure - self.store.account_exposure(user_id, mode)
        budget = min(budget, headroom)
        budget_note = f"your per-bot limit ({_plain(limits.max_bot_allocation)})"
        if headroom < limits.max_bot_allocation:
            budget_note = f"the room left under your total exposure limit ({_plain(headroom)})"
        try:
            balance = (await connector.balances()).get(rules.quote)
        except ExchangeError:
            balance = None
        if balance is not None:
            spendable = balance.available - limits.reserve_balance
            if spendable < budget:
                budget = spendable
                budget_note = (
                    f"your available {rules.quote} ({_plain(balance.available)}) "
                    f"minus your reserve ({_plain(limits.reserve_balance)})"
                )
        else:
            notes.append(f"Your {rules.quote} balance could not be read, so only your risk limits were used.")
        cent = Decimal("0.01")
        budget = max(budget, Decimal(0)).quantize(cent, rounding=ROUND_DOWN)
        minimum_order = rules.min_notional * Decimal("1.1")
        while levels > MIN_LEVELS and budget / (levels - 1) < minimum_order:
            levels -= 1
        order_value = min(budget / (levels - 1), limits.max_order_value).quantize(cent, rounding=ROUND_DOWN)
        if order_value < minimum_order:
            order_value = minimum_order.quantize(cent, rounding=ROUND_UP)
        allocation = min(budget, order_value * (levels - 1)).quantize(cent, rounding=ROUND_DOWN)
        allocation = max(allocation, order_value)
        stop = rules.floor_price(lower * Decimal("0.95"))

        values: dict[str, Any] = {
            "mode": mode,
            "symbol": symbol,
            "name": f"{rules.base} Grid",
            "lower_price": _plain(lower),
            "upper_price": _plain(upper),
            "levels": levels,
            "order_value": _plain(order_value),
            "max_allocation": _plain(allocation),
            "stop_price": _plain(stop),
            "trailing_trigger": "1",
            "trailing_confirm_seconds": 300,
            "trailing_max_recenters": 3,
            "trailing_ceiling": _plain(rules.floor_price(upper * Decimal("1.5"))),
        }
        plan = await self.plan(user_id, {**values, "trailing_enabled": False})
        for _ in range(5):
            if plan["valid"] or not any("minimum" in e or "too small" in e for e in plan["errors"]):
                break
            bumped = (Decimal(values["order_value"]) * Decimal("1.2")).quantize(cent, rounding=ROUND_UP)
            if bumped > limits.max_order_value:
                break
            values["order_value"] = _plain(bumped)
            values["max_allocation"] = _plain(max(Decimal(values["max_allocation"]), bumped))
            plan = await self.plan(user_id, {**values, "trailing_enabled": False})

        spacing = min(
            (Decimal(row["sell_price"]) / Decimal(row["buy_price"]) - 1 for row in plan["slots"]),
            default=Decimal(0),
        )
        places_now = sum(1 for row in plan["slots"] if row["places_now"])
        notes.append(
            f"{levels} grid lines give {levels - 1} slots about {spacing * 100:.2f}% apart, "
            f"enough to profit after {fee * 2 * 100:.2f}% round-trip fees. "
            f"{places_now} buys would be placed below the current price at the start."
        )
        notes.append(f"Budget is limited by {budget_note}.")
        notes.append("The stop loss is 5% below the lowest grid line. Review every value before creating the bot.")
        errors = list(plan["errors"])
        needed = Decimal(values["order_value"]) * (levels - 1)
        if budget < Decimal(values["order_value"]):
            errors.append(
                f"Your available budget ({_plain(budget)} {rules.quote}) is below one "
                f"{values['order_value']} {rules.quote} order. Add funds, lower other "
                "exposure, or raise your limits first."
            )
        elif budget < needed:
            notes.append(
                f"The allocation ({values['max_allocation']}) is less than the full grid "
                f"({_plain(needed)}), so buys pause once it is in use."
            )
        return {
            "values": values,
            "valid": plan["valid"] and not errors,
            "errors": errors,
            "notes": notes,
            "last_price": _plain(price),
        }

    async def create(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        plan = await self.plan(user_id, values)
        if not plan["valid"]:
            raise TradingError("; ".join(plan["errors"]))
        bot = self.store.create_bot(user_id, plan["bot"])
        rules = await self.live.connector(user_id, bot["mode"]).market_rules(bot["symbol"])
        self.store.create_slots(
            bot["id"], 1,
            self.ladder(rules, _dec(bot["lower_price"]), _dec(bot["upper_price"]), bot["levels"]),
        )
        return self.summary(user_id, bot["id"])

    # ------------------------------------------------------------ lifecycle
    def _bot(self, user_id: int, bot_id: int) -> dict[str, Any]:
        bot = self.store.get_bot(user_id, bot_id)
        if bot is None:
            raise TradingError("Bot not found")
        return bot

    def _unresolved(self, bot: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.list_intents(
            bot["user_id"], bot["mode"], ("submission_unknown", "needs_review"), bot["id"]
        )

    def start(self, user_id: int, bot_id: int) -> dict[str, Any]:
        bot = self._bot(user_id, bot_id)
        if bot["status"] not in ("armed", "paused", "halted"):
            raise TradingError(f"A {bot['status']} bot cannot be started")
        settings = self.store.settings(user_id)
        if settings["kill_switch"]:
            raise TradingError("Turn off the kill switch first")
        if bot["mode"] == "live" and not settings["live_enabled"]:
            raise TradingError("Enable live trading in Settings first")
        if self._unresolved(bot):
            raise TradingError("Resolve this bot's orders that need review first")
        running = [
            other for other in self.store.list_bots(user_id, RUNNING_STATES)
            if other["mode"] == bot["mode"] and other["id"] != bot["id"]
        ]
        if len(running) >= settings["limits"].max_running_bots:
            raise TradingError(
                f"The maximum of {settings['limits'].max_running_bots} running bots is reached"
            )
        bot = self.store.update_bot(
            bot, status="running", halt_reason=None, consecutive_errors=0,
            trigger_started_at=None,
        )
        self.store.bot_event(bot_id, user_id, "started", "Bot started")
        return self.summary(user_id, bot_id)

    def pause(self, user_id: int, bot_id: int) -> dict[str, Any]:
        bot = self._bot(user_id, bot_id)
        if bot["status"] != "running":
            raise TradingError("Only a running bot can be paused")
        self.store.update_bot(bot, status="paused", halt_reason="Paused by user")
        self.store.bot_event(bot_id, user_id, "paused", "Paused by user; open orders remain")
        return self.summary(user_id, bot_id)

    def stop(self, user_id: int, bot_id: int, liquidate: bool) -> dict[str, Any]:
        bot = self._bot(user_id, bot_id)
        if bot["status"] in ("stopped", "stopping"):
            raise TradingError(f"The bot is already {bot['status']}")
        mode = "liquidate" if liquidate else "cancel_only"
        self.store.update_bot(bot, status="stopping", stop_mode=mode, halt_reason="Stopping")
        self.store.bot_event(bot_id, user_id, "stopping", f"Stop requested ({mode})")
        return self.summary(user_id, bot_id)

    def delete(self, user_id: int, bot_id: int) -> None:
        bot = self._bot(user_id, bot_id)
        if bot["status"] not in ("armed", "stopped"):
            raise TradingError("Stop the bot before deleting it")
        if self.store.list_intents(user_id, bot["mode"], ACTIVE_INTENT_STATES, bot_id):
            raise TradingError("The bot still has active orders")
        self.store.delete_bot(user_id, bot_id)

    def _halt(self, bot: dict[str, Any], reason: str) -> dict[str, Any]:
        if bot["status"] in ("halted", "stopped"):
            return bot
        self.store.bot_event(bot["id"], bot["user_id"], "halted", reason)
        return self.store.update_bot(bot, status="halted", halt_reason=reason)

    def _notice(self, bot: dict[str, Any], message: str) -> None:
        if bot.get("last_notice") != message:
            self.store.bot_event(bot["id"], bot["user_id"], "notice", message)
            self.store.update_bot(bot, last_notice=message)

    # ---------------------------------------------------------------- sync
    def sync(self, bot: dict[str, Any]) -> dict[str, Any]:
        """Applies terminal order outcomes to slots exactly once."""
        fee = self.live.fee_rate
        realized = Decimal(0)
        for slot in self.store.slots(bot["id"]):
            if slot["state"] == "buy_open" and slot["buy_intent_id"]:
                intent = self.store.get_intent(bot["user_id"], slot["buy_intent_id"])
                if intent is None or intent["state"] in ACTIVE_INTENT_STATES:
                    continue
                executed = _dec(intent["executed_quantity"])
                held = _dec(slot["held_quantity"]) + executed
                cost = _dec(slot["cost_basis"]) + executed * _dec(intent["price"]) * (1 + fee)
                self.store.update_slot(
                    slot, buy_intent_id=None, held_quantity=str(held), cost_basis=str(cost),
                    state="holding" if held > 0 else "idle",
                )
            elif slot["state"] == "sell_open" and slot["sell_intent_id"]:
                intent = self.store.get_intent(bot["user_id"], slot["sell_intent_id"])
                if intent is None or intent["state"] in ACTIVE_INTENT_STATES:
                    continue
                pnl, held, cost = self._apply_sale(slot, intent)
                realized += pnl
                self.store.update_slot(
                    slot, sell_intent_id=None, held_quantity=str(held), cost_basis=str(cost),
                    state="holding" if held > 0 else "idle",
                )
        if bot["liquidation_intent_id"]:
            intent = self.store.get_intent(bot["user_id"], bot["liquidation_intent_id"])
            if intent is not None and intent["state"] not in ACTIVE_INTENT_STATES:
                remaining = _dec(intent["executed_quantity"])
                price = _dec(intent["price"])
                for slot in self.store.slots(bot["id"]):
                    if remaining <= 0 or slot["state"] != "holding":
                        continue
                    held = _dec(slot["held_quantity"])
                    take = min(held, remaining)
                    cost = _dec(slot["cost_basis"])
                    cost_part = cost * take / held if held > 0 else Decimal(0)
                    realized += take * price * (1 - fee) - cost_part
                    remaining -= take
                    left = held - take
                    self.store.update_slot(
                        slot, held_quantity=str(left), cost_basis=str(cost - cost_part),
                        state="holding" if left > 0 else "idle",
                    )
                bot = self.store.update_bot(bot, liquidation_intent_id=None)
        if realized != 0:
            self.store.record_pnl(bot["user_id"], bot["mode"], bot["id"], realized)
            bot = self.store.update_bot(
                bot, realized_pnl=str(_dec(bot["realized_pnl"]) + realized)
            )
        return bot

    def _apply_sale(
        self, slot: dict[str, Any], intent: dict[str, Any]
    ) -> tuple[Decimal, Decimal, Decimal]:
        executed = _dec(intent["executed_quantity"])
        held = _dec(slot["held_quantity"])
        cost = _dec(slot["cost_basis"])
        if executed <= 0 or held <= 0:
            return Decimal(0), held, cost
        sold = min(executed, held)
        cost_part = cost * sold / held
        proceeds = sold * _dec(intent["price"]) * (1 - self.live.fee_rate)
        return proceeds - cost_part, held - sold, cost - cost_part

    def bot_used(self, bot: dict[str, Any]) -> Decimal:
        used = Decimal(0)
        for intent in self.store.list_intents(
            bot["user_id"], bot["mode"], ACTIVE_INTENT_STATES, bot["id"]
        ):
            if intent["side"] == "buy":
                used += _dec(intent["price"]) * (
                    _dec(intent["quantity"]) - _dec(intent["executed_quantity"])
                )
        for slot in self.store.slots(bot["id"]):
            if slot["state"] in ("holding", "sell_open"):
                used += _dec(slot["cost_basis"])
        return used

    def _fresh(self, bot: dict[str, Any]) -> bool:
        cutoff = now_ms() - RECONCILE_FRESHNESS_MS
        return all(
            intent["last_reconciled_at"] and int(intent["last_reconciled_at"]) >= cutoff
            for intent in self.store.list_intents(
                bot["user_id"], bot["mode"], ACTIVE_STATES_FOR_BOT, bot["id"]
            )
        )

    # ---------------------------------------------------------------- tick
    async def run_cycle(self) -> None:
        for bot in self.store.list_bots(None, TICK_STATES):
            try:
                await self.tick(bot)
            except (TradingError, ExchangeError, ValueError) as exc:
                fresh = self.store.get_bot(bot["user_id"], bot["id"])
                if fresh is not None:
                    self._notice(fresh, f"Cycle error: {exc}")
            except Exception as exc:  # noqa: BLE001 - fail safe: halt, never continue blindly
                fresh = self.store.get_bot(bot["user_id"], bot["id"])
                if fresh is not None:
                    self._halt(fresh, f"Unexpected engine error ({type(exc).__name__}); review before resuming")

    async def tick(self, bot: dict[str, Any]) -> None:
        bot = self.sync(bot)
        if self._unresolved(bot):
            self._halt(bot, "An order outcome is unknown or needs review")
            return
        if bot["status"] in ("paused", "halted"):
            return
        user_id, mode = bot["user_id"], bot["mode"]
        settings = self.store.settings(user_id)
        if settings["kill_switch"]:
            self._halt(bot, "Kill switch engaged")
            return
        if mode == "live" and not settings["live_enabled"]:
            self._halt(bot, "Live trading is disabled")
            return
        limits = settings["limits"]
        try:
            rules = await self.live.connector(user_id, mode).market_rules(bot["symbol"])
        except ExchangeError as exc:
            errors = int(bot["consecutive_errors"]) + 1
            bot = self.store.update_bot(bot, consecutive_errors=errors)
            if errors >= limits.max_consecutive_errors:
                self._halt(bot, f"Market data unavailable {errors} times: {exc}")
            return
        if bot["consecutive_errors"]:
            bot = self.store.update_bot(bot, consecutive_errors=0)
        bot = self.store.update_bot(bot, last_tick_at=now_ms())
        if bot["status"] == "stopping":
            await self._progress_stop(bot, rules)
            return
        if bot["status"] == "recentering":
            await self._progress_recenter(bot, rules)
            return
        if not rules.active:
            self._notice(bot, f"{bot['symbol']} is not tradable right now")
            return
        if rules.last_price <= _dec(bot["stop_price"]):
            self.store.bot_event(
                bot["id"], user_id, "stop_loss",
                f"Price {rules.last_price} reached the stop {bot['stop_price']}",
            )
            self.store.update_bot(
                bot, status="stopping", stop_mode="liquidate",
                halt_reason="Stop-loss triggered",
            )
            return
        if await self._check_trailing(bot, rules):
            return
        if not self._fresh(bot):
            self._notice(bot, "Waiting for every open order to be verified with the exchange")
            return
        await self._place_orders(bot, rules)

    async def _place_orders(self, bot: dict[str, Any], rules: MarketRules) -> None:
        writes = 0
        for phase in ("sell", "buy"):
            for slot in self.store.slots(bot["id"]):
                if writes >= WRITES_PER_TICK:
                    return
                if phase == "sell":
                    eligible = slot["state"] == "holding" and not slot["sell_intent_id"]
                else:
                    eligible = (
                        slot["state"] == "idle"
                        and int(slot["ladder_version"]) == int(bot["ladder_version"])
                        and _dec(slot["buy_price"]) < rules.last_price
                        and (rules.best_ask is None or _dec(slot["buy_price"]) < rules.best_ask)
                    )
                if not eligible:
                    continue
                placer = self._place_sell if phase == "sell" else self._place_buy
                if not await placer(bot, slot, rules):
                    continue
                writes += 1
                # A submission may have halted the bot (unknown outcome or
                # credential rejection). Never continue with stale state.
                current = self.store.get_bot(bot["user_id"], bot["id"])
                if current is None or current["status"] != "running":
                    return
                bot = current

    async def _place_buy(self, bot: dict[str, Any], slot: dict[str, Any], rules: MarketRules) -> bool:
        price = _dec(slot["buy_price"])
        quantity = rules.floor_quantity(_dec(bot["order_value"]) / price)
        context, limits = await self.live.build_context(
            bot["user_id"], bot["mode"], rules, "buy", price, quantity,
            bot=bot, bot_used=self.bot_used(bot),
        )
        decision = evaluate_order(context, limits)
        if not decision.allowed:
            self._notice(bot, "Buy skipped: " + "; ".join(decision.failures()))
            return False
        intent = self.store.create_intent(
            bot["user_id"], bot["mode"], "grid_buy", bot["symbol"], "buy", price, quantity,
            bot_id=bot["id"], slot_id=slot["id"],
        )
        self.store.update_slot(slot, state="buy_open", buy_intent_id=intent["id"])
        await self.live.submit_intent(intent)
        return True

    async def _place_sell(self, bot: dict[str, Any], slot: dict[str, Any], rules: MarketRules) -> bool:
        held = _dec(slot["held_quantity"])
        balances = await self.live.connector(bot["user_id"], bot["mode"]).balances()
        available = balances[rules.base].available if rules.base in balances else Decimal(0)
        quantity = rules.floor_quantity(min(held, available))
        if quantity <= 0 or quantity < held * Decimal("0.98"):
            self._halt(
                bot,
                f"Holdings mismatch: the slot holds {held} {rules.base} but only "
                f"{available} is available",
            )
            return False
        price = _dec(slot["sell_price"])
        context, limits = await self.live.build_context(
            bot["user_id"], bot["mode"], rules, "sell", price, quantity,
            bot=bot, enforce_price_band=False,
        )
        decision = evaluate_order(context, limits)
        if not decision.allowed:
            self._notice(bot, "Sell skipped: " + "; ".join(decision.failures()))
            return False
        if quantity != held:
            self.store.update_slot(slot, held_quantity=str(quantity))
        intent = self.store.create_intent(
            bot["user_id"], bot["mode"], "grid_sell", bot["symbol"], "sell", price, quantity,
            bot_id=bot["id"], slot_id=slot["id"],
        )
        self.store.update_slot(slot, state="sell_open", sell_intent_id=intent["id"])
        await self.live.submit_intent(intent)
        return True

    async def _cancel_bot_orders(self, bot: dict[str, Any], side: str | None) -> bool:
        """Requests cancellation; returns True when no matching order remains active."""
        active = [
            intent for intent in self.store.list_intents(
                bot["user_id"], bot["mode"], ACTIVE_INTENT_STATES, bot["id"]
            )
            if side is None or intent["side"] == side
        ]
        for intent in active:
            if intent["state"] in ("open", "partially_filled", "cancel_requested") and intent["exchange_id"]:
                try:
                    await self.live.cancel_intent(bot["user_id"], intent["id"])
                except TradingError:
                    continue
        remaining = [
            intent for intent in self.store.list_intents(
                bot["user_id"], bot["mode"], ACTIVE_INTENT_STATES, bot["id"]
            )
            if side is None or intent["side"] == side
        ]
        return not remaining

    async def _progress_stop(self, bot: dict[str, Any], rules: MarketRules) -> None:
        if not await self._cancel_bot_orders(bot, None):
            return
        bot = self.sync(bot)
        holdings = [slot for slot in self.store.slots(bot["id"]) if slot["state"] == "holding"]
        total = sum((_dec(slot["held_quantity"]) for slot in holdings), Decimal(0))
        dust = rules.floor_quantity(total) <= 0 or total * rules.last_price < max(rules.min_notional, Decimal("0.01"))
        if bot["stop_mode"] != "liquidate" or not holdings or dust:
            detail = (
                "Stopped; holdings were kept in your account"
                if holdings and bot["stop_mode"] != "liquidate"
                else "Stopped"
            )
            self.store.update_bot(bot, status="stopped", halt_reason=detail)
            self.store.bot_event(bot["id"], bot["user_id"], "stopped", detail)
            return
        if bot["liquidation_intent_id"]:
            return
        reference = rules.best_bid or rules.last_price
        price = rules.floor_price(reference * (1 - LIQUIDATION_DISCOUNT))
        balances = await self.live.connector(bot["user_id"], bot["mode"]).balances()
        available = balances[rules.base].available if rules.base in balances else Decimal(0)
        quantity = rules.floor_quantity(min(total, available))
        context, limits = await self.live.build_context(
            bot["user_id"], bot["mode"], rules, "sell", price, quantity,
            bot=bot, enforce_price_band=False,
        )
        decision = evaluate_order(context, limits)
        if not decision.allowed:
            self._notice(bot, "Liquidation blocked: " + "; ".join(decision.failures()))
            return
        intent = self.store.create_intent(
            bot["user_id"], bot["mode"], "liquidation", bot["symbol"], "sell", price, quantity,
            bot_id=bot["id"],
        )
        self.store.update_bot(bot, liquidation_intent_id=intent["id"])
        self.store.bot_event(
            bot["id"], bot["user_id"], "liquidating",
            f"Selling {quantity} {rules.base} at {price} or better",
        )
        await self.live.submit_intent(intent)

    # ------------------------------------------------------------- trailing
    async def _check_trailing(self, bot: dict[str, Any], rules: MarketRules) -> bool:
        if not bot["trailing_enabled"]:
            return False
        upper = _dec(bot["upper_price"])
        trigger = upper * (1 + _dec(bot["trailing_trigger"]))
        if rules.last_price <= trigger:
            if bot["trigger_started_at"]:
                self.store.update_bot(bot, trigger_started_at=None)
            return False
        ceiling = _dec(bot["trailing_ceiling"])
        if upper >= ceiling or int(bot["recenter_count"]) >= int(bot["trailing_max_recenters"]):
            reason = (
                "Trailing ceiling reached" if upper >= ceiling
                else "Maximum Trailing Up recenters reached"
            )
            self.store.update_bot(bot, status="paused", halt_reason=reason)
            self.store.bot_event(bot["id"], bot["user_id"], "paused", reason)
            return True
        started = bot["trigger_started_at"]
        if not started:
            self.store.update_bot(bot, trigger_started_at=now_ms())
            return False
        if now_ms() - int(started) < int(bot["trailing_confirm_seconds"]) * 1000:
            return False
        self.store.update_bot(bot, status="recentering", halt_reason="Trailing Up")
        self.store.bot_event(
            bot["id"], bot["user_id"], "recentering",
            f"Price {rules.last_price} held above {trigger}; moving the grid up",
        )
        return True

    async def _progress_recenter(self, bot: dict[str, Any], rules: MarketRules) -> None:
        if not await self._cancel_bot_orders(bot, "buy"):
            return
        bot = self.sync(bot)
        if self._unresolved(bot):
            self._halt(bot, "An order outcome is unknown during Trailing Up")
            return
        lower, upper = _dec(bot["lower_price"]), _dec(bot["upper_price"])
        width = upper - lower
        spacing = width / (int(bot["levels"]) - 1)
        new_upper = rules.floor_price(min(_dec(bot["trailing_ceiling"]), rules.last_price + spacing))
        shift = new_upper - upper
        if shift <= 0:
            self.store.update_bot(bot, status="running", halt_reason=None, trigger_started_at=None)
            return
        new_lower = rules.floor_price(new_upper - width)
        old_stop = _dec(bot["stop_price"])
        new_stop = max(old_stop, rules.floor_price(old_stop + shift))
        version = int(bot["ladder_version"]) + 1
        self.store.delete_idle_slots(bot["id"], int(bot["ladder_version"]))
        self.store.create_slots(bot["id"], version, self.ladder(rules, new_lower, new_upper, int(bot["levels"])))
        self.store.update_bot(
            bot, status="running", halt_reason=None, lower_price=str(new_lower),
            upper_price=str(new_upper), stop_price=str(new_stop), ladder_version=version,
            recenter_count=int(bot["recenter_count"]) + 1, trigger_started_at=None,
        )
        self.store.bot_event(
            bot["id"], bot["user_id"], "recentered",
            f"Grid moved to {new_lower}-{new_upper}; stop raised to {new_stop}; "
            "existing holdings keep their sell targets",
        )

    # -------------------------------------------------------------- summary
    def summary(self, user_id: int, bot_id: int) -> dict[str, Any]:
        bot = self._bot(user_id, bot_id)
        slots = self.store.slots(bot_id)
        held = sum((_dec(slot["held_quantity"]) for slot in slots), Decimal(0))
        cost = sum(
            (_dec(slot["cost_basis"]) for slot in slots if slot["state"] in ("holding", "sell_open")),
            Decimal(0),
        )
        counts: dict[str, int] = {}
        for slot in slots:
            counts[slot["state"]] = counts.get(slot["state"], 0) + 1
        return {
            **bot,
            "hosted_by": "BitsBots on this PC",
            "slot_counts": counts,
            "held_quantity": str(held),
            "holding_cost": str(cost),
            "committed": str(self.bot_used(bot)),
            "slots": slots,
            "events": self.store.bot_events(user_id, bot_id, limit=20),
        }

    def summaries(self, user_id: int) -> list[dict[str, Any]]:
        return [self.summary(user_id, bot["id"]) for bot in self.store.list_bots(user_id)]

    def activity(self, user_id: int, bot_id: int) -> dict[str, Any]:
        """Bot summary plus every order it placed, with fill times for charting."""
        summary = self.summary(user_id, bot_id)
        orders = self.store.list_intents(user_id, bot_id=bot_id, limit=1000)
        filled_at = self.store.fill_times(user_id, orders)
        for order in orders:
            order["filled_at"] = filled_at.get(int(order["id"]))
        return {"bot": summary, "orders": orders, "fee_rate": str(self.live.fee_rate)}
