"""Order previews, confirmation, submission, reconciliation, and cancellation.

Safety rules enforced here:

* A preview is stored server-side and expires; confirmation uses only the
  stored normalized values, so the browser cannot alter a confirmed order.
* Risk checks run at preview time and again with fresh data at confirmation.
* An intent (with its unique client order ID) is persisted and the preview is
  consumed in one transaction before any network call.
* An ambiguous submission is never retried. It is reconciled by looking the
  order up; unresolved or conflicting evidence halts automation for review.
* Emergency cancel only touches orders created by BitsBots, never other
  orders on the account (such as native NonKYC grid bots).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from . import __version__
from .config import Settings
from .dry_run import DryRunExchange, DryRunFaults
from .exchange import (
    ExchangeAmbiguous,
    ExchangeAuthError,
    ExchangeConnector,
    ExchangeError,
    ExchangeNotFound,
    ExchangeOrder,
    ExchangeRejected,
    MarketRules,
    OrderRequest,
    is_eligible_symbol,
    to_decimal,
)
from .live_account import LiveAccountError, decrypt_secret
from .live_store import LiveStore, now_ms
from .nonkyc_trading import NONKYC_CAPABILITIES, NonKycPublicRules, NonKycTradingClient
from .risk import OrderContext, RiskDecision, RiskLimits, evaluate_order
from .storage import Storage

MODES = ("dry_run", "live")
PREVIEW_TTL_MS = 60_000
RECONCILE_WINDOW_MS = 5 * 60_000
ENABLE_PHRASE = "ENABLE LIVE TRADING"
REFRESHABLE_STATES = ("open", "partially_filled", "cancel_requested")
# Exchange-acknowledged orders whose state must be verified recently before a
# bot may place anything new.
ACTIVE_STATES_FOR_BOT = REFRESHABLE_STATES


class TradingError(ValueError):
    """User-facing refusal (validation, risk, or state)."""


def _decimal_input(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise TradingError(f"{label} must be a number") from exc
    if not result.is_finite() or result <= 0:
        raise TradingError(f"{label} must be greater than zero")
    return result


def validate_public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise TradingError("Enter a valid public IP address") from exc
    if not address.is_global:
        raise TradingError(
            "That is a private or local address. Enter the public IP your "
            "internet provider assigns (the address NonKYC sees)."
        )
    return str(address)


class LiveTradingService:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        rules_source: NonKycPublicRules | None = None,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.store = LiveStore(storage.connection)
        self.rules_source = rules_source or NonKycPublicRules(settings)
        self.fee_rate = Decimal(str(settings.fee_rate))
        self._connectors: dict[tuple[int, str], ExchangeConnector] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self.dry_run_faults: dict[int, DryRunFaults] = {}

    async def close(self) -> None:
        for connector in self._connectors.values():
            await connector.close()
        self._connectors.clear()
        await self.rules_source.close()

    # ------------------------------------------------------------ connectors
    def _lock(self, user_id: int, mode: str) -> asyncio.Lock:
        return self._locks.setdefault((user_id, mode), asyncio.Lock())

    def connector(self, user_id: int, mode: str) -> ExchangeConnector:
        if mode not in MODES:
            raise TradingError("Mode must be dry_run or live")
        key = (user_id, mode)
        if key in self._connectors:
            return self._connectors[key]
        if mode == "dry_run":
            connector: ExchangeConnector = DryRunExchange(
                self.store,
                user_id,
                self.rules_source,
                self.fee_rate,
                self.dry_run_faults.setdefault(user_id, DryRunFaults()),
            )
        else:
            stored = self.storage.get_exchange_credentials(user_id, "nonkyc")
            if stored is None:
                raise TradingError("Connect a NonKYC API key in Settings first")
            access_key, encrypted_secret = stored
            try:
                secret = decrypt_secret(encrypted_secret, self.settings)
            except LiveAccountError as exc:
                raise TradingError(str(exc)) from exc
            connector = NonKycTradingClient(access_key, secret, self.settings)
        self._connectors[key] = connector
        return connector

    async def credentials_changed(self, user_id: int) -> None:
        """Any key change disables live trading and halts live bots."""
        connector = self._connectors.pop((user_id, "live"), None)
        if connector is not None:
            await connector.close()
        self.store.update_settings(user_id, live_enabled=False)
        self.halt_bots(user_id, "live", "NonKYC credentials changed; review and re-enable")

    def halt_bots(self, user_id: int, mode: str | None, reason: str) -> int:
        count = 0
        for bot in self.store.list_bots(user_id, ("running", "stopping", "recentering")):
            if mode is not None and bot["mode"] != mode:
                continue
            self.store.update_bot(bot, status="halted", halt_reason=reason)
            self.store.bot_event(bot["id"], user_id, "halted", reason)
            count += 1
        return count

    # --------------------------------------------------------------- settings
    def status(self, user_id: int) -> dict[str, Any]:
        settings = self.store.settings(user_id)
        limits: RiskLimits = settings["limits"]
        return {
            "live_enabled": settings["live_enabled"],
            "enabled_at": settings["enabled_at"],
            "kill_switch": settings["kill_switch"],
            "ip_address": settings["ip_address"],
            "ip_confirmed_at": settings["ip_confirmed_at"],
            "permissions_confirmed_at": settings["permissions_confirmed_at"],
            "risk_acknowledged_at": settings["risk_acknowledged_at"],
            "credentials_connected": self.storage.get_exchange_credentials(user_id) is not None,
            "limits": limits.to_dict(),
            "capabilities": {
                "nonkyc": {
                    "native_grid_bots": NONKYC_CAPABILITIES.native_grid_bots,
                    "testnet": NONKYC_CAPABILITIES.testnet,
                    "market_orders": NONKYC_CAPABILITIES.market_orders,
                    "notes": list(NONKYC_CAPABILITIES.notes),
                }
            },
            "modes": {
                mode: {
                    "exposure": str(self.store.account_exposure(user_id, mode)),
                    "daily_realized_pnl": str(self.store.daily_realized_pnl(user_id, mode)),
                    "open_orders": self.store.open_order_count(user_id, mode),
                    "daily_orders": self.store.daily_order_count(user_id, mode),
                }
                for mode in MODES
            },
            "dry_run_balances": self.store.dry_run_balances(user_id),
            "fee_rate": str(self.fee_rate),
            "enable_phrase": ENABLE_PHRASE,
        }

    def update_limits(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        current = self.store.settings(user_id)["limits"].to_dict()
        try:
            limits = RiskLimits.from_dict({**current, **values})
        except (ValueError, TypeError) as exc:
            raise TradingError(str(exc)) from exc
        self.store.update_settings(user_id, limits=limits)
        return self.status(user_id)

    async def enable_live(
        self,
        user_id: int,
        ip_address: str,
        trade_permission_confirmed: bool,
        withdrawals_disabled_confirmed: bool,
        ip_allowlisted_confirmed: bool,
        risk_acknowledged: bool,
        typed_phrase: str,
    ) -> dict[str, Any]:
        if typed_phrase.strip() != ENABLE_PHRASE:
            raise TradingError(f'Type "{ENABLE_PHRASE}" exactly to enable live trading')
        if not all(
            (
                trade_permission_confirmed,
                withdrawals_disabled_confirmed,
                ip_allowlisted_confirmed,
                risk_acknowledged,
            )
        ):
            raise TradingError("Confirm every key-permission, IP, and risk statement")
        address = validate_public_ip(ip_address)
        connector = self.connector(user_id, "live")
        try:
            await connector.balances()
        except ExchangeAuthError as exc:
            raise TradingError(
                "NonKYC rejected this key. Check its trading permission and that "
                f"{address} is on its IP allowlist."
            ) from exc
        except ExchangeError as exc:
            raise TradingError(f"Could not verify the NonKYC key: {exc}") from exc
        timestamp = now_ms()
        self.store.update_settings(
            user_id,
            live_enabled=True,
            enabled_at=timestamp,
            ip_address=address,
            ip_confirmed_at=timestamp,
            permissions_confirmed_at=timestamp,
            risk_acknowledged_at=timestamp,
            app_version=__version__,
        )
        return self.status(user_id)

    def disable_live(self, user_id: int) -> dict[str, Any]:
        self.store.update_settings(user_id, live_enabled=False)
        self.halt_bots(user_id, "live", "Live trading was disabled")
        return self.status(user_id)

    def confirm_ip(self, user_id: int, ip_address: str) -> dict[str, Any]:
        address = validate_public_ip(ip_address)
        self.store.update_settings(user_id, ip_address=address, ip_confirmed_at=now_ms())
        return self.status(user_id)

    def set_kill_switch(self, user_id: int, engaged: bool) -> dict[str, Any]:
        self.store.update_settings(user_id, kill_switch=engaged)
        if engaged:
            self.halt_bots(user_id, None, "Kill switch engaged")
        return self.status(user_id)

    # ---------------------------------------------------------------- risk
    async def build_context(
        self,
        user_id: int,
        mode: str,
        rules: MarketRules,
        side: str,
        price: Decimal,
        quantity: Decimal,
        bot: dict[str, Any] | None = None,
        bot_used: Decimal | None = None,
        enforce_price_band: bool = True,
    ) -> tuple[OrderContext, RiskLimits]:
        settings = self.store.settings(user_id)
        balances = await self.connector(user_id, mode).balances()
        quote_balance = balances.get(rules.quote)
        base_balance = balances.get(rules.base)
        limits: RiskLimits = settings["limits"]
        context = OrderContext(
            mode=mode,
            live_enabled=settings["live_enabled"],
            kill_switch=settings["kill_switch"],
            rules=rules,
            side=side,
            price=price,
            quantity=quantity,
            quote_available=quote_balance.available if quote_balance else Decimal(0),
            base_available=base_balance.available if base_balance else Decimal(0),
            account_exposure=self.store.account_exposure(user_id, mode),
            open_orders=self.store.open_order_count(user_id, mode),
            daily_orders=self.store.daily_order_count(user_id, mode),
            daily_realized_pnl=self.store.daily_realized_pnl(user_id, mode),
            market_age_seconds=max(0.0, (now_ms() - rules.fetched_at) / 1000),
            fee_rate=self.fee_rate,
            bot_used=bot_used,
            bot_allocation=(
                min(to_decimal(bot["max_allocation"]), limits.max_bot_allocation)
                if bot
                else None
            ),
            enforce_price_band=enforce_price_band,
        )
        return context, limits

    async def _rules(self, user_id: int, mode: str, symbol: str) -> MarketRules:
        try:
            return await self.connector(user_id, mode).market_rules(symbol)
        except ExchangeError as exc:
            raise TradingError(str(exc)) from exc

    # ------------------------------------------------------------- previews
    async def preview_order(
        self,
        user_id: int,
        mode: str,
        symbol: str,
        side: str,
        price: Any,
        quantity: Any,
    ) -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        if not is_eligible_symbol(normalized_symbol):
            raise TradingError("Only COIN/USDT and COIN/USDC markets can be traded")
        if side not in {"buy", "sell"}:
            raise TradingError("Side must be buy or sell")
        entered_price = _decimal_input(price, "Price")
        entered_quantity = _decimal_input(quantity, "Quantity")
        rules = await self._rules(user_id, mode, normalized_symbol)
        normalized_price = rules.floor_price(entered_price)
        normalized_quantity = rules.floor_quantity(entered_quantity)
        if normalized_price <= 0 or normalized_quantity <= 0:
            raise TradingError("Price or quantity is below the market's smallest step")
        try:
            context, limits = await self.build_context(
                user_id, mode, rules, side, normalized_price, normalized_quantity
            )
        except ExchangeError as exc:
            raise TradingError(f"Could not read account balances: {exc}") from exc
        decision = evaluate_order(context, limits)
        notional = normalized_price * normalized_quantity
        fee = notional * self.fee_rate
        warnings: list[str] = []
        if normalized_price != entered_price or normalized_quantity != entered_quantity:
            warnings.append(
                f"Rounded down to market precision: price {normalized_price}, "
                f"quantity {normalized_quantity}"
            )
        if side == "buy" and rules.best_ask and normalized_price >= rules.best_ask:
            warnings.append("This buy is at or above the best ask and will likely fill immediately")
        if side == "sell" and rules.best_bid and normalized_price <= rules.best_bid:
            warnings.append("This sell is at or below the best bid and will likely fill immediately")
        if mode == "live":
            warnings.append("LIVE: this places a real order with real funds on NonKYC")
        preview_id = secrets.token_hex(16)
        created_at = now_ms()
        fingerprint = hashlib.sha256(
            json.dumps(
                [user_id, mode, normalized_symbol, side, str(normalized_price),
                 str(normalized_quantity), limits.to_dict()],
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.store.save_preview(
            {
                "id": preview_id,
                "user_id": user_id,
                "mode": mode,
                "symbol": normalized_symbol,
                "side": side,
                "price": str(normalized_price),
                "quantity": str(normalized_quantity),
                "fingerprint": fingerprint,
                "allowed": int(decision.allowed),
                "risk_json": json.dumps(decision.to_dict()),
                "expires_at": created_at + PREVIEW_TTL_MS,
                "created_at": created_at,
            }
        )
        return {
            "id": preview_id,
            "mode": mode,
            "symbol": normalized_symbol,
            "base": rules.base,
            "quote": rules.quote,
            "side": side,
            "entered_price": str(entered_price),
            "entered_quantity": str(entered_quantity),
            "price": str(normalized_price),
            "quantity": str(normalized_quantity),
            "notional": str(notional),
            "estimated_fee": str(fee),
            "total": str(notional + fee if side == "buy" else notional - fee),
            "quote_available": str(context.quote_available),
            "base_available": str(context.base_available),
            "last_price": str(rules.last_price),
            "best_bid": str(rules.best_bid) if rules.best_bid else None,
            "best_ask": str(rules.best_ask) if rules.best_ask else None,
            "allowed": decision.allowed,
            "risk": decision.to_dict(),
            "warnings": warnings,
            "expires_at": created_at + PREVIEW_TTL_MS,
        }

    async def confirm_order(self, user_id: int, preview_id: str) -> dict[str, Any]:
        preview = self.store.get_preview(user_id, preview_id)
        if preview is None:
            raise TradingError("Preview not found")
        if preview["consumed_at"] is not None:
            raise TradingError("This preview was already confirmed")
        if now_ms() > int(preview["expires_at"]):
            raise TradingError("This preview expired. Create a new preview.")
        if not preview["allowed"]:
            raise TradingError("This preview failed risk checks and cannot be confirmed")
        mode = preview["mode"]
        price = Decimal(preview["price"])
        quantity = Decimal(preview["quantity"])
        rules = await self._rules(user_id, mode, preview["symbol"])
        try:
            context, limits = await self.build_context(
                user_id, mode, rules, preview["side"], price, quantity
            )
        except ExchangeError as exc:
            raise TradingError(f"Could not re-check balances: {exc}") from exc
        decision = evaluate_order(context, limits)
        if not decision.allowed:
            raise TradingError(
                "Conditions changed since the preview: " + "; ".join(decision.failures())
            )
        try:
            intent = self.store.create_intent(
                user_id, mode, "manual", preview["symbol"], preview["side"],
                price, quantity, preview_id=preview_id,
            )
        except ValueError as exc:
            raise TradingError(str(exc)) from exc
        return await self.submit_intent(intent)

    # ----------------------------------------------------------- submission
    async def submit_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        user_id, mode = intent["user_id"], intent["mode"]
        async with self._lock(user_id, mode):
            current = self.store.get_intent(user_id, intent["id"])
            if current is None or current["state"] != "pending":
                return current or intent
            settings = self.store.settings(user_id)
            if settings["kill_switch"] or (mode == "live" and not settings["live_enabled"]):
                return self.store.update_intent(
                    current, "blocked", "Trading was disabled before submission",
                    state="rejected", error="Trading disabled before submission",
                )
            current = self.store.update_intent(
                current, "submitting", "Sending order to exchange",
                state="submitting", submitted_at=now_ms(),
            )
            request = OrderRequest(
                symbol=current["symbol"],
                side=current["side"],
                price=Decimal(current["price"]),
                quantity=Decimal(current["quantity"]),
                client_id=current["client_id"],
            )
            try:
                order = await self.connector(user_id, mode).place_order(request)
            except ExchangeAmbiguous as exc:
                self._halt_owner(current, f"Order {current['client_id']} outcome unknown")
                return self.store.update_intent(
                    current, "submission_unknown", str(exc),
                    state="submission_unknown", error=str(exc),
                )
            except ExchangeRejected as exc:
                if isinstance(exc, ExchangeAuthError):
                    self.halt_bots(user_id, mode, "Exchange rejected credentials or IP")
                return self.store.update_intent(
                    current, "rejected", str(exc), state="rejected", error=str(exc)
                )
            except ExchangeError as exc:
                self._halt_owner(current, f"Order {current['client_id']} outcome unknown")
                return self.store.update_intent(
                    current, "submission_unknown", str(exc),
                    state="submission_unknown", error=str(exc),
                )
            return self._apply_order(current, order, "acknowledged")

    def _apply_order(
        self, intent: dict[str, Any], order: ExchangeOrder, event: str
    ) -> dict[str, Any]:
        return self.store.update_intent(
            intent,
            event,
            f"{order.status} executed {order.executed_quantity}/{order.quantity}",
            state=order.status,
            exchange_id=order.exchange_id,
            executed_quantity=str(order.executed_quantity),
            exchange_status=order.raw_status,
            last_reconciled_at=now_ms(),
        )

    def _halt_owner(self, intent: dict[str, Any], reason: str) -> None:
        if intent.get("bot_id") is None:
            return
        bot = self.store.get_bot(intent["user_id"], intent["bot_id"])
        if bot and bot["status"] not in {"halted", "stopped"}:
            self.store.update_bot(bot, status="halted", halt_reason=reason)
            self.store.bot_event(bot["id"], bot["user_id"], "halted", reason)

    # -------------------------------------------------------- reconciliation
    async def refresh_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        if intent["state"] == "submission_unknown":
            return await self.reconcile_unknown(intent)
        if intent["state"] not in REFRESHABLE_STATES or not intent["exchange_id"]:
            return intent
        try:
            order = await self.connector(intent["user_id"], intent["mode"]).get_order(
                intent["exchange_id"]
            )
        except ExchangeNotFound:
            self._halt_owner(intent, "An open order disappeared from the exchange")
            return self.store.update_intent(
                intent, "needs_review", "Exchange no longer reports this order",
                state="needs_review",
            )
        except ExchangeError:
            return intent
        if intent["state"] == "cancel_requested" and not order.is_terminal:
            return self.store.update_intent(
                intent, "refreshed", "Cancel pending", executed_quantity=str(order.executed_quantity),
                last_reconciled_at=now_ms(),
            )
        if order.status == intent["state"] and str(order.executed_quantity) == intent["executed_quantity"]:
            return self.store.update_intent(intent, "refreshed", "No change", last_reconciled_at=now_ms())
        return self._apply_order(intent, order, "refreshed")

    async def reconcile_unknown(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Determines whether an ambiguous submission created an order. Never
        resubmits. Conflicting evidence escalates to needs_review."""
        connector = self.connector(intent["user_id"], intent["mode"])
        client_id = intent["client_id"]
        try:
            order = await connector.get_order(client_id)
            if order.client_id == client_id and order.symbol == intent["symbol"]:
                return self._apply_order(intent, order, "reconciled")
        except ExchangeNotFound:
            pass
        except ExchangeError:
            return intent
        submitted_at = int(intent["submitted_at"] or intent["created_at"])
        try:
            candidates = await connector.list_orders(
                intent["symbol"], since=submitted_at - 60_000, limit=200
            )
        except ExchangeError:
            return intent
        by_client = [order for order in candidates if order.client_id == client_id]
        if len(by_client) == 1:
            return self._apply_order(intent, by_client[0], "reconciled")
        if len(by_client) > 1:
            self._halt_owner(intent, "Duplicate exchange orders share one client ID")
            return self.store.update_intent(
                intent, "needs_review", "Multiple exchange orders share this client ID",
                state="needs_review",
            )
        linked = {
            row["exchange_id"]
            for row in self.store.list_intents(intent["user_id"], intent["mode"], limit=1000)
            if row["exchange_id"]
        }
        lookalikes = [
            order
            for order in candidates
            if order.exchange_id not in linked
            and order.side == intent["side"]
            and order.price == Decimal(intent["price"])
            and order.quantity == Decimal(intent["quantity"])
            and order.created_at <= submitted_at + 120_000
        ]
        if lookalikes:
            self._halt_owner(intent, "An unlinked matching order needs manual review")
            return self.store.update_intent(
                intent, "needs_review",
                "An unlinked order with the same side, price, and quantity exists",
                state="needs_review",
            )
        if now_ms() - submitted_at < RECONCILE_WINDOW_MS:
            return self.store.update_intent(
                intent, "reconcile_pending", "No order found yet; will check again",
                last_reconciled_at=now_ms(),
            )
        return self.store.update_intent(
            intent, "not_found",
            "No matching order after the reconciliation window; treated as not placed",
            state="not_found",
        )

    async def resolve_review(
        self, user_id: int, intent_id: int, exchange_id: str | None
    ) -> dict[str, Any]:
        """Manual resolution: link an intent to an exchange order the user has
        verified, or mark it as not placed."""
        intent = self.store.get_intent(user_id, intent_id)
        if intent is None or intent["state"] not in ("needs_review", "submission_unknown"):
            raise TradingError("Only orders awaiting review can be resolved")
        if not exchange_id:
            return self.store.update_intent(
                intent, "resolved", "User confirmed no exchange order exists",
                state="not_found",
            )
        try:
            order = await self.connector(user_id, intent["mode"]).get_order(exchange_id)
        except ExchangeError as exc:
            raise TradingError(f"Could not read order {exchange_id}: {exc}") from exc
        if order.symbol != intent["symbol"] or order.side != intent["side"]:
            raise TradingError("That exchange order does not match this intent")
        return self._apply_order(intent, order, "resolved")

    # ----------------------------------------------------------- cancellation
    async def cancel_intent(self, user_id: int, intent_id: int) -> dict[str, Any]:
        intent = self.store.get_intent(user_id, intent_id)
        if intent is None:
            raise TradingError("Order not found")
        if intent["state"] not in ("open", "partially_filled", "cancel_requested"):
            raise TradingError(f"An order that is {intent['state']} cannot be cancelled")
        async with self._lock(user_id, intent["mode"]):
            intent = self.store.update_intent(
                intent, "cancel_requested", "Cancellation requested", state="cancel_requested"
            )
            try:
                await self.connector(user_id, intent["mode"]).cancel_order(intent["exchange_id"])
            except ExchangeNotFound:
                pass
            except ExchangeAmbiguous as exc:
                return self.store.update_intent(intent, "cancel_unknown", str(exc), error=str(exc))
            except ExchangeError as exc:
                self.store.update_intent(intent, "cancel_failed", str(exc), error=str(exc))
        refreshed = self.store.get_intent(user_id, intent_id)
        assert refreshed is not None
        return await self.refresh_intent(refreshed)

    async def emergency_cancel(self, user_id: int, mode: str) -> dict[str, Any]:
        """Engages the kill switch, halts bots, and cancels only BitsBots orders."""
        self.set_kill_switch(user_id, True)
        results = {"cancelled": 0, "failed": []}
        for intent in self.store.list_intents(
            user_id, mode, ("open", "partially_filled", "cancel_requested"), limit=1000
        ):
            try:
                updated = await self.cancel_intent(user_id, intent["id"])
                if updated["state"] in ("cancelled", "cancel_requested"):
                    results["cancelled"] += 1
            except TradingError as exc:
                results["failed"].append({"id": intent["id"], "error": str(exc)})
        return results

    # ------------------------------------------------------------ lifecycle
    def recover_on_startup(self) -> None:
        """Runs before any new order: interrupted submissions become unknown
        (they may have reached the exchange); never-sent intents are closed."""
        for intent in self.store.intents_in_states(("pending",)):
            self.store.update_intent(
                intent, "interrupted", "Interrupted before submission; nothing was sent",
                state="rejected", error="Interrupted before submission",
            )
        for intent in self.store.intents_in_states(("submitting",)):
            self._halt_owner(intent, "App restarted during order submission")
            self.store.update_intent(
                intent, "submission_unknown", "App restarted during submission",
                state="submission_unknown",
            )

    async def maintenance_cycle(self) -> None:
        for intent in self.store.intents_in_states(
            ("open", "partially_filled", "cancel_requested", "submission_unknown")
        ):
            try:
                await self.refresh_intent(intent)
            except TradingError:
                continue
