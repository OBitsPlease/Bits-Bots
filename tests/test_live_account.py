from pathlib import Path

import pytest

from bitsbots.config import Settings
from bitsbots.live_account import (
    NonKycAccountClient,
    decrypt_secret,
    encrypt_secret,
    fetch_live_account_snapshot,
    fetch_live_grid_bots,
    mask_access_key,
)
from bitsbots.models import Market
from bitsbots.nonkyc import NonKycClient


def make_settings(tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "bitsbots.db")


def test_encrypt_decrypt_roundtrip_uses_local_key_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    token = encrypt_secret("super-secret-value", settings)
    assert token != "super-secret-value"
    assert decrypt_secret(token, settings) == "super-secret-value"
    assert (tmp_path / "local.key").exists()


def test_decrypt_secret_rejects_tampered_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    token = encrypt_secret("super-secret-value", settings)
    with pytest.raises(Exception):
        decrypt_secret(token[:-2] + "zz", settings)


def test_mask_access_key_hides_the_middle() -> None:
    assert mask_access_key("abcd1234efgh5678") == "abcd…5678"
    assert mask_access_key("short") == "•••••"


def test_signature_is_deterministic_hmac_sha256() -> None:
    client = NonKycAccountClient("access", "secret", Settings())
    signature_one = client._sign("access/balances1700000000000")
    signature_two = client._sign("access/balances1700000000000")
    assert signature_one == signature_two
    assert len(signature_one) == 64


def test_headers_sign_the_full_request_url_not_just_the_path() -> None:
    # NonKYC's own reference client signs access_key + FULL URL (base URL +
    # path + query string) + nonce. Signing only the relative path produces
    # a signature NonKYC rejects with 401/403, even for a valid key.
    settings = Settings()
    client = NonKycAccountClient("access-key", "secret-key", settings)
    full_url = f"{settings.api_base_url}/balances"
    headers = client._headers(full_url)
    nonce = headers["X-API-NONCE"]
    expected_signature = client._sign("access-key" + full_url + nonce)
    assert headers["X-API-SIGN"] == expected_signature
    wrong_signature = client._sign("access-key" + "/balances" + nonce)
    assert headers["X-API-SIGN"] != wrong_signature


async def test_fetch_live_account_snapshot_normalizes_payloads(monkeypatch) -> None:
    async def fake_balances(self):
        return [
            {"asset": "BTC", "available": "0.5", "held": "0", "pending": "0.1"},
            {"asset": "USDT", "available": "0", "held": "0", "pending": "0"},
        ]

    async def fake_open_orders(self, limit: int = 50):
        return [
            {
                "id": "1",
                # NonKYC nests the traded pair under "market", not a
                # top-level "symbol" field.
                "market": {"id": "m1", "symbol": "BTC/USDT"},
                "side": "buy",
                "type": "limit",
                "price": "50000",
                "quantity": "0.01",
                "remainQuantity": "0.01",
                "status": "active",
                "createdAt": 1700000000000,
            }
        ]

    async def fake_trades(self, limit: int = 25):
        return [
            {
                "id": "9",
                "market": {"id": "m1", "symbol": "BTC/USDT"},
                "side": "sell",
                "price": "49000",
                "quantity": "0.02",
                "fee": "0.5",
                "createdAt": 1700000000000,
            }
        ]

    async def fake_close(self):
        return None

    monkeypatch.setattr(NonKycAccountClient, "get_balances", fake_balances)
    monkeypatch.setattr(NonKycAccountClient, "get_open_orders", fake_open_orders)
    monkeypatch.setattr(NonKycAccountClient, "get_recent_trades", fake_trades)
    monkeypatch.setattr(NonKycAccountClient, "close", fake_close)

    snapshot = await fetch_live_account_snapshot("access", "secret", Settings())

    assert snapshot["balances"] == [{"asset": "BTC", "available": 0.5, "held": 0.1, "total": 0.6}]
    assert snapshot["open_orders"][0]["symbol"] == "BTC/USDT"
    assert snapshot["open_orders"][0]["quantity"] == 0.01
    assert snapshot["recent_trades"][0]["symbol"] == "BTC/USDT"
    assert snapshot["recent_trades"][0]["fee"] == 0.5


async def test_fetch_live_grid_bots_reconstructs_grid_stats_per_market(monkeypatch) -> None:
    async def fake_all_orders_any_status(self, page_size: int = 100, max_pages: int = 50):
        orders = [
            {
                "userProvidedId": f"gb:bot1:{i}",
                "market": {"symbol": "BTC/USDT"},
                "side": "buy",
                "price": price,
                "quantity": "1",
                "executedQuantity": "0",
                "remainQuantity": "1",
                "isActive": True,
            }
            for i, price in enumerate(("95", "100", "105"))
        ]
        orders.append(
            {
                "userProvidedId": "gb:bot1:3",
                "market": {"symbol": "BTC/USDT"},
                "side": "buy",
                "price": "90",
                "quantity": "1",
                "executedQuantity": "1",
                "remainQuantity": "0",
                "isActive": False,
            }
        )
        orders.append(
            {
                "userProvidedId": "gb:bot1:4",
                "market": {"symbol": "BTC/USDT"},
                "side": "sell",
                "price": "110",
                "quantity": "1",
                "executedQuantity": "1",
                "remainQuantity": "0",
                "isActive": False,
            }
        )
        return orders

    async def fake_get_markets(self):
        return [Market(symbol="BTC/USDT", last_price=105, high_price=110, low_price=90,
                        base_volume=1, quote_volume=100, last_trade_at=0, active=True)]

    async def fake_get_all_trades(self, page_size: int = 100, max_pages: int = 10):
        return [
            {"market": {"symbol": "BTC/USDT"}, "side": "buy", "price": "90", "quantity": "1"},
            {"market": {"symbol": "BTC/USDT"}, "side": "sell", "price": "110", "quantity": "1"},
        ]

    async def fake_close(self):
        return None

    monkeypatch.setattr(NonKycAccountClient, "get_all_orders_any_status", fake_all_orders_any_status)
    monkeypatch.setattr(NonKycAccountClient, "get_all_trades", fake_get_all_trades)
    monkeypatch.setattr(NonKycAccountClient, "close", fake_close)
    monkeypatch.setattr(NonKycClient, "get_markets", fake_get_markets)
    monkeypatch.setattr(NonKycClient, "close", fake_close)

    result = await fetch_live_grid_bots("access", "secret", Settings())

    assert result["markets_traded"] == 1
    bot = result["bots"][0]
    assert bot["bot_id"] == "bot1"
    assert bot["symbol"] == "BTC/USDT"
    assert bot["active_orders"] == 3
    assert bot["fills"] == 2
    # Bought at 90, sold at 110 -> booked profit 20
    assert round(bot["grid_profit"], 2) == 20.0
    assert result["bots_running"] == 1
    assert result["other_activity"] == []


async def test_fetch_live_grid_bots_treats_untagged_orders_as_other_activity(monkeypatch) -> None:
    async def fake_all_orders_any_status(self, page_size: int = 100, max_pages: int = 50):
        return [
            {
                # No "gb:" userProvidedId - this is manual trading, not a
                # real bot, even though it's the only order for this market.
                "market": {"symbol": "ETH/USDT"},
                "side": "buy",
                "price": "1000",
                "quantity": "1",
                "executedQuantity": "0",
                "remainQuantity": "1",
                "isActive": True,
            }
        ]

    async def fake_get_markets(self):
        return []

    async def fake_get_all_trades(self, page_size: int = 100, max_pages: int = 10):
        return []

    async def fake_close(self):
        return None

    monkeypatch.setattr(NonKycAccountClient, "get_all_orders_any_status", fake_all_orders_any_status)
    monkeypatch.setattr(NonKycAccountClient, "get_all_trades", fake_get_all_trades)
    monkeypatch.setattr(NonKycAccountClient, "close", fake_close)
    monkeypatch.setattr(NonKycClient, "get_markets", fake_get_markets)
    monkeypatch.setattr(NonKycClient, "close", fake_close)

    result = await fetch_live_grid_bots("access", "secret", Settings())

    assert result["bots"] == []
    assert result["markets_traded"] == 0
    assert len(result["other_activity"]) == 1
    assert result["other_activity"][0]["symbol"] == "ETH/USDT"


async def test_fetch_live_grid_bots_keeps_paused_bot_with_no_open_orders(monkeypatch) -> None:
    """A grid bot that's paused/exhausted may have 0 open orders left, but
    its filled/cancelled history still carries the "gb:" tag, so it should
    still show up as a bot (just STOPPED), not disappear or get lumped in
    with manual trading."""

    async def fake_all_orders_any_status(self, page_size: int = 100, max_pages: int = 50):
        orders = []
        for i in range(12):
            side = "buy" if i % 2 == 0 else "sell"
            orders.append(
                {
                    "userProvidedId": f"gb:bot2:{i}",
                    "market": {"symbol": "ALPHA/USDT"},
                    "side": side,
                    "price": "1",
                    "quantity": "1",
                    "executedQuantity": "1",
                    "remainQuantity": "0",
                    "isActive": False,
                }
            )
        return orders

    async def fake_get_markets(self):
        return []

    async def fake_get_all_trades(self, page_size: int = 100, max_pages: int = 10):
        return []

    async def fake_close(self):
        return None

    monkeypatch.setattr(NonKycAccountClient, "get_all_orders_any_status", fake_all_orders_any_status)
    monkeypatch.setattr(NonKycAccountClient, "get_all_trades", fake_get_all_trades)
    monkeypatch.setattr(NonKycAccountClient, "close", fake_close)
    monkeypatch.setattr(NonKycClient, "get_markets", fake_get_markets)
    monkeypatch.setattr(NonKycClient, "close", fake_close)

    result = await fetch_live_grid_bots("access", "secret", Settings())

    assert len(result["bots"]) == 1
    assert result["bots"][0]["bot_id"] == "bot2"
    assert result["bots"][0]["symbol"] == "ALPHA/USDT"


async def test_fetch_live_grid_bots_uses_trade_history_for_fills_when_market_is_unambiguous(
    monkeypatch,
) -> None:
    """NonKYC's /getorders?status=all endpoint prunes superseded grid
    rungs, so it under-counts real fills. When a market belongs to exactly
    one bot, fills/P&L should be reconstructed from full trade history
    instead, which is not pruned the same way."""

    async def fake_all_orders_any_status(self, page_size: int = 100, max_pages: int = 50):
        # Only 2 orders survive in order history (both still "active"), even
        # though many more trades actually happened on this bot's market.
        return [
            {
                "userProvidedId": "gb:bot3:0",
                "market": {"symbol": "ALPHA/USDT"},
                "side": "buy",
                "price": "1.0",
                "quantity": "10",
                "executedQuantity": "0",
                "remainQuantity": "10",
                "isActive": True,
            },
            {
                "userProvidedId": "gb:bot3:1",
                "market": {"symbol": "ALPHA/USDT"},
                "side": "sell",
                "price": "1.2",
                "quantity": "10",
                "executedQuantity": "0",
                "remainQuantity": "10",
                "isActive": True,
            },
        ]

    async def fake_get_all_trades(self, page_size: int = 100, max_pages: int = 10):
        # Trade history retains far more fills than the pruned order list.
        return [
            {"market": {"symbol": "ALPHA/USDT"}, "side": "buy", "price": "1.0", "quantity": "5"},
            {"market": {"symbol": "ALPHA/USDT"}, "side": "sell", "price": "1.1", "quantity": "5"},
            {"market": {"symbol": "ALPHA/USDT"}, "side": "buy", "price": "0.9", "quantity": "5"},
        ]

    async def fake_get_markets(self):
        return []

    async def fake_close(self):
        return None

    monkeypatch.setattr(NonKycAccountClient, "get_all_orders_any_status", fake_all_orders_any_status)
    monkeypatch.setattr(NonKycAccountClient, "get_all_trades", fake_get_all_trades)
    monkeypatch.setattr(NonKycAccountClient, "close", fake_close)
    monkeypatch.setattr(NonKycClient, "get_markets", fake_get_markets)
    monkeypatch.setattr(NonKycClient, "close", fake_close)

    result = await fetch_live_grid_bots("access", "secret", Settings())

    assert len(result["bots"]) == 1
    bot = result["bots"][0]
    # 3 real trades, not the 0 you'd get from the (pruned) order list.
    assert bot["fills"] == 3
    assert bot["fills_are_estimated"] is False
    # Invested is the notional of the two still-active orders, regardless
    # of side: (1.0 * 10) + (1.2 * 10).
    assert bot["invested"] == pytest.approx(22.0)

