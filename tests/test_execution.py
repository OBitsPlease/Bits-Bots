from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bitsbots import execution as execution_module
from bitsbots import nonkyc_trading
from bitsbots.config import SETTINGS
from bitsbots.exchange import (
    ExchangeAmbiguous,
    ExchangeAuthError,
    ExchangeRejected,
    ExchangeUnavailable,
    MarketRules,
    OrderRequest,
    is_eligible_symbol,
)
from bitsbots.execution import LiveTradingService, TradingError, validate_public_ip
from bitsbots.live_store import now_ms
from bitsbots.nonkyc_trading import NonKycTradingClient
from bitsbots.risk import OrderContext, RiskLimits, evaluate_order
from bitsbots.storage import Storage


class FakeRules:
    def __init__(self) -> None:
        self.prices: dict[str, Decimal] = {}

    def rules(self, symbol: str) -> MarketRules:
        base, quote = symbol.split("/")
        last = self.prices.get(symbol, Decimal("1.0000"))
        return MarketRules(
            symbol=symbol, base=base, quote=quote, price_decimals=4,
            quantity_decimals=2, min_quantity=Decimal("0"), min_notional=Decimal("1"),
            last_price=last, best_bid=last - Decimal("0.0010"),
            best_ask=last + Decimal("0.0010"), active=True, buys_paused=False,
            sells_paused=False, fetched_at=now_ms(),
        )

    async def __call__(self, symbol: str) -> MarketRules:
        return self.rules(symbol)

    async def close(self) -> None:
        return None


@pytest.fixture
def env(tmp_path: Path):
    settings = replace(SETTINGS, database_path=tmp_path / "live.db")
    storage = Storage(settings.database_path)
    user = storage.create_user("live@example.com", "Live Tester", "hash")
    rules = FakeRules()
    service = LiveTradingService(storage, settings, rules_source=rules)  # type: ignore[arg-type]
    yield service, user.id, rules, storage
    storage.close()


async def test_preview_never_creates_an_order_and_rounds_to_precision(env) -> None:
    service, user_id, _, _ = env
    preview = await service.preview_order(user_id, "dry_run", "abc/usdt", "buy", "0.90009", "10.555")
    assert preview["allowed"] is True
    assert preview["price"] == "0.9000"
    assert preview["quantity"] == "10.55"
    assert any("Rounded down" in warning for warning in preview["warnings"])
    assert service.store.dry_run_orders(user_id) == []
    assert service.store.list_intents(user_id) == []


async def test_confirm_submits_once_and_duplicate_confirmation_is_refused(env) -> None:
    service, user_id, _, _ = env
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    assert intent["state"] == "open"
    assert intent["client_id"].startswith("bb") and len(intent["client_id"]) < 32
    with pytest.raises(TradingError, match="already"):
        await service.confirm_order(user_id, preview["id"])
    assert len(service.store.dry_run_orders(user_id)) == 1
    assert len(service.store.list_intents(user_id)) == 1


async def test_expired_and_foreign_previews_are_refused(env, monkeypatch) -> None:
    service, user_id, _, storage = env
    other = storage.create_user("other@example.com", "Other", "hash")
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    with pytest.raises(TradingError, match="not found"):
        await service.confirm_order(other.id, preview["id"])
    later = now_ms() + execution_module.PREVIEW_TTL_MS + 1
    monkeypatch.setattr(execution_module, "now_ms", lambda: later)
    with pytest.raises(TradingError, match="expired"):
        await service.confirm_order(user_id, preview["id"])


async def test_risk_failures_and_kill_switch_block_confirmation(env) -> None:
    service, user_id, _, _ = env
    too_large = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "100")
    assert too_large["allowed"] is False
    assert any(check["name"] == "max_order_value" and not check["passed"] for check in too_large["risk"]["checks"])
    with pytest.raises(TradingError, match="failed risk"):
        await service.confirm_order(user_id, too_large["id"])
    service.set_kill_switch(user_id, True)
    blocked = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    assert blocked["allowed"] is False


async def test_ineligible_markets_and_live_without_credentials_are_refused(env) -> None:
    service, user_id, _, _ = env
    assert not is_eligible_symbol("USDC/USDT")
    assert not is_eligible_symbol("BTC/ETH")
    with pytest.raises(TradingError, match="COIN/USDT"):
        await service.preview_order(user_id, "dry_run", "BTC/ETH", "buy", "1", "1")
    with pytest.raises(TradingError, match="Connect a NonKYC API key"):
        await service.preview_order(user_id, "live", "ABC/USDT", "buy", "0.9", "10")


async def test_marketable_buy_fills_and_moves_balances(env) -> None:
    service, user_id, _, _ = env
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "1.0000", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    assert intent["state"] == "filled"
    balances = await service.connector(user_id, "dry_run").balances()
    assert balances["ABC"].available == Decimal("10")
    assert balances["USDT"].available == Decimal("1000") - Decimal("10") * Decimal("1.002")


async def test_ambiguous_accepted_order_is_linked_not_duplicated(env) -> None:
    service, user_id, _, _ = env
    service.dry_run_faults.setdefault(user_id, execution_module.DryRunFaults())
    service.connector(user_id, "dry_run")
    service.dry_run_faults[user_id].ambiguous_accept_next = True
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    assert intent["state"] == "submission_unknown"
    reconciled = await service.refresh_intent(intent)
    assert reconciled["state"] == "open"
    assert reconciled["exchange_id"]
    assert len(service.store.dry_run_orders(user_id)) == 1


async def test_ambiguous_dropped_order_waits_then_is_not_found(env, monkeypatch) -> None:
    service, user_id, _, _ = env
    service.connector(user_id, "dry_run")
    service.dry_run_faults[user_id].ambiguous_drop_next = True
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    still_unknown = await service.refresh_intent(intent)
    assert still_unknown["state"] == "submission_unknown"
    later = now_ms() + execution_module.RECONCILE_WINDOW_MS + 1
    monkeypatch.setattr(execution_module, "now_ms", lambda: later)
    resolved = await service.refresh_intent(still_unknown)
    assert resolved["state"] == "not_found"
    assert service.store.dry_run_orders(user_id) == []


async def test_partial_fill_then_cancel_keeps_executed_quantity(env) -> None:
    service, user_id, rules, _ = env
    service.connector(user_id, "dry_run")
    service.dry_run_faults[user_id].partial_fill_ratio = Decimal("0.5")
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    rules.prices["ABC/USDT"] = Decimal("0.8900")
    partial = await service.refresh_intent(intent)
    assert partial["state"] == "partially_filled"
    assert Decimal(partial["executed_quantity"]) == Decimal("5")
    rules.prices["ABC/USDT"] = Decimal("1.0000")
    cancelled = await service.cancel_intent(user_id, intent["id"])
    assert cancelled["state"] == "cancelled"
    assert Decimal(cancelled["executed_quantity"]) == Decimal("5")


async def test_restart_recovery_marks_interrupted_submissions_unknown(env) -> None:
    service, user_id, _, _ = env
    pending = service.store.create_intent(user_id, "dry_run", "manual", "ABC/USDT", "buy", Decimal("0.9"), Decimal("10"))
    sending = service.store.create_intent(user_id, "dry_run", "manual", "ABC/USDT", "buy", Decimal("0.9"), Decimal("10"))
    service.store.update_intent(sending, "submitting", "test", state="submitting", submitted_at=now_ms())
    service.recover_on_startup()
    assert service.store.get_intent(user_id, pending["id"])["state"] == "rejected"
    assert service.store.get_intent(user_id, sending["id"])["state"] == "submission_unknown"


async def test_emergency_cancel_engages_kill_switch_and_cancels_bitsbots_orders(env) -> None:
    service, user_id, _, _ = env
    preview = await service.preview_order(user_id, "dry_run", "ABC/USDT", "buy", "0.9", "10")
    intent = await service.confirm_order(user_id, preview["id"])
    result = await service.emergency_cancel(user_id, "dry_run")
    assert result["cancelled"] == 1
    assert service.store.settings(user_id)["kill_switch"] is True
    assert service.store.get_intent(user_id, intent["id"])["state"] == "cancelled"


def test_public_ip_validation_rejects_local_addresses() -> None:
    assert validate_public_ip(" 8.8.8.8 ") == "8.8.8.8"
    for address in ("192.168.1.5", "10.0.0.2", "127.0.0.1", "not-an-ip"):
        with pytest.raises(TradingError):
            validate_public_ip(address)


def test_risk_allows_protective_sells_after_daily_loss_limit() -> None:
    rules = FakeRules().rules("ABC/USDT")
    base = dict(
        mode="dry_run", live_enabled=False, kill_switch=False, rules=rules,
        price=Decimal("1.0000"), quantity=Decimal("5"), quote_available=Decimal("100"),
        base_available=Decimal("5"), account_exposure=Decimal("0"), open_orders=0,
        daily_orders=0, daily_realized_pnl=Decimal("-50"), market_age_seconds=1,
        fee_rate=Decimal("0.002"),
    )
    limits = RiskLimits()
    assert not evaluate_order(OrderContext(side="buy", **base), limits).allowed
    assert evaluate_order(OrderContext(side="sell", **base), limits).allowed


def test_risk_limits_reject_inconsistent_values() -> None:
    with pytest.raises(ValueError):
        RiskLimits.from_dict({"max_bot_allocation": "500", "max_total_exposure": "100"})
    with pytest.raises(ValueError):
        RiskLimits.from_dict({"max_order_value": "0"})


def _client(handler) -> NonKycTradingClient:
    return NonKycTradingClient(
        "access", "secret", SETTINGS, transport=httpx.MockTransport(handler)
    )


async def test_nonkyc_create_order_signs_exact_body_and_uses_strict_limit(monkeypatch) -> None:
    monkeypatch.setattr(nonkyc_trading, "WRITE_INTERVAL_SECONDS", 0)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        captured["headers"] = request.headers
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "id": "x1", "userProvidedId": "bbabc1", "market": {"symbol": "ABC/USDT"},
            "side": "buy", "price": "0.9", "quantity": "10", "executedQuantity": "0",
            "status": "Active", "isActive": True, "createdAt": 1,
        })

    client = _client(handler)
    order = await client.place_order(
        OrderRequest("ABC/USDT", "buy", Decimal("0.9000"), Decimal("10.00"), "bbabc1")
    )
    await client.close()
    body = captured["body"]
    assert '"strictValidate":true' in body and '"type":"limit"' in body
    assert '"userProvidedId":"bbabc1"' in body and '"price":"0.9"' in body
    expected = client._sign(
        "access" + captured["url"] + body + captured["headers"]["X-API-NONCE"]
    )
    assert captured["headers"]["X-API-SIGN"] == expected
    assert order.status == "open" and order.client_id == "bbabc1"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(504, json={"error": {"code": 504, "message": "Gateway Timeout"}}), ExchangeAmbiguous),
        (httpx.Response(400, json={"error": {"code": 20001, "message": "Insufficient funds"}}), ExchangeRejected),
        (httpx.Response(401, json={"error": {"code": 1002, "message": "Authorisation failed"}}), ExchangeAuthError),
    ],
)
async def test_nonkyc_write_responses_are_classified(monkeypatch, response, error) -> None:
    monkeypatch.setattr(nonkyc_trading, "WRITE_INTERVAL_SECONDS", 0)
    client = _client(lambda request: response)
    with pytest.raises(error):
        await client.place_order(OrderRequest("ABC/USDT", "buy", Decimal("1"), Decimal("1"), "bb1"))
    await client.close()


@pytest.mark.parametrize(
    ("failure", "error"),
    [(httpx.ReadTimeout, ExchangeAmbiguous), (httpx.ConnectError, ExchangeUnavailable)],
)
async def test_nonkyc_transport_failures_distinguish_sent_from_unsent(monkeypatch, failure, error) -> None:
    monkeypatch.setattr(nonkyc_trading, "WRITE_INTERVAL_SECONDS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("simulated", request=request)

    client = _client(handler)
    with pytest.raises(error):
        await client.place_order(OrderRequest("ABC/USDT", "buy", Decimal("1"), Decimal("1"), "bb1"))
    await client.close()


def test_credentials_migrate_to_exchange_accounts(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    storage = Storage(path)
    user = storage.create_user("legacy@example.com", "Legacy", "hash")
    storage.connection.execute(
        "INSERT INTO exchange_credentials VALUES (?, 'key', 'secret', 1, 1)", (user.id,)
    )
    storage.connection.commit()
    storage.close()
    migrated = Storage(path)
    assert migrated.get_exchange_credentials(user.id) == ("key", "secret")
    legacy_rows = migrated.connection.execute("SELECT COUNT(*) FROM exchange_credentials").fetchone()[0]
    assert legacy_rows == 0
    migrated.close()
    assert sqlite3  # keep import explicit for readers
