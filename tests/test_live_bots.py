from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bitsbots import live_bots as live_bots_module
from bitsbots.config import SETTINGS
from bitsbots.exchange import MarketRules
from bitsbots.execution import LiveTradingService, TradingError
from bitsbots.live_bots import MIN_LEVELS, GridEngine
from bitsbots.live_store import now_ms
from bitsbots.storage import Storage


class FakeRules:
    def __init__(self) -> None:
        self.price = Decimal("1.0000")

    async def __call__(self, symbol: str) -> MarketRules:
        base, quote = symbol.split("/")
        return MarketRules(
            symbol=symbol, base=base, quote=quote, price_decimals=4, quantity_decimals=2,
            min_quantity=Decimal("0"), min_notional=Decimal("1"), last_price=self.price,
            best_bid=self.price - Decimal("0.0010"), best_ask=self.price + Decimal("0.0010"),
            active=True, buys_paused=False, sells_paused=False, fetched_at=now_ms(),
        )

    async def close(self) -> None:
        return None


@pytest.fixture
def grid(tmp_path: Path):
    settings = replace(SETTINGS, database_path=tmp_path / "grid.db")
    storage = Storage(settings.database_path)
    user = storage.create_user("grid@example.com", "Grid Tester", "hash")
    rules = FakeRules()
    live = LiveTradingService(storage, settings, rules_source=rules)  # type: ignore[arg-type]
    engine = GridEngine(live)
    yield live, engine, rules, user.id
    storage.close()


def bot_values(**overrides) -> dict:
    values = {
        "mode": "dry_run", "symbol": "ABC/USDT", "lower_price": "0.90",
        "upper_price": "1.10", "levels": 5, "order_value": "5",
        "max_allocation": "20", "stop_price": "0.85",
    }
    values.update(overrides)
    return values


async def cycle(live: LiveTradingService, engine: GridEngine) -> None:
    await live.maintenance_cycle()
    await engine.run_cycle()


def buy_intents(live, user_id, state=None):
    return [
        intent for intent in live.store.list_intents(user_id, "dry_run", limit=500)
        if intent["side"] == "buy" and (state is None or intent["state"] == state)
    ]


async def test_plan_rejects_unsafe_grids(grid) -> None:
    _, engine, _, user_id = grid
    tight = await engine.plan(user_id, bot_values(levels=50))
    assert not tight["valid"]
    assert any("round-trip fees" in error for error in tight["errors"])
    bad_stop = await engine.plan(user_id, bot_values(stop_price="0.95"))
    assert not bad_stop["valid"]
    valid = await engine.plan(user_id, bot_values())
    assert valid["valid"]
    assert [row["places_now"] for row in valid["slots"]] == [True, True, False, False]


async def test_armed_bot_places_nothing_until_started(grid) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values())
    assert bot["status"] == "armed"
    await cycle(live, engine)
    assert live.store.list_intents(user_id) == []
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    prices = sorted(intent["price"] for intent in buy_intents(live, user_id, "open"))
    assert prices == ["0.9000", "0.9500"]


async def test_full_grid_cycle_books_profit_after_fees(grid) -> None:
    live, engine, rules, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    rules.price = Decimal("0.9400")
    await cycle(live, engine)
    sells = [i for i in live.store.list_intents(user_id) if i["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["price"] == "1.0000"
    rules.price = Decimal("1.0100")
    await cycle(live, engine)
    await cycle(live, engine)
    summary = engine.summary(user_id, bot["id"])
    assert Decimal(summary["realized_pnl"]) > 0
    rebuy = [i for i in buy_intents(live, user_id, "open") if i["price"] == "0.9500"]
    assert len(rebuy) == 1


async def test_allocation_caps_buys(grid) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values(max_allocation="6"))
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    await cycle(live, engine)
    assert len(buy_intents(live, user_id)) == 1
    assert Decimal(engine.summary(user_id, bot["id"])["committed"]) <= Decimal("6")


async def test_stop_loss_cancels_and_liquidates(grid) -> None:
    live, engine, rules, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    rules.price = Decimal("0.8400")
    for _ in range(4):
        await cycle(live, engine)
    final = engine.summary(user_id, bot["id"])
    assert final["status"] == "stopped"
    assert Decimal(final["held_quantity"]) == 0
    assert Decimal(final["realized_pnl"]) < 0
    assert any(i["purpose"] == "liquidation" and i["state"] == "filled" for i in live.store.list_intents(user_id))


async def test_unknown_order_outcome_halts_bot(grid) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    live.connector(user_id, "dry_run")
    live.dry_run_faults[user_id].ambiguous_drop_next = True
    await cycle(live, engine)
    await cycle(live, engine)
    assert engine.summary(user_id, bot["id"])["status"] == "halted"
    assert len(live.store.list_intents(user_id)) == 1
    with pytest.raises(TradingError, match="need review"):
        engine.start(user_id, bot["id"])


async def test_kill_switch_halts_running_bots(grid) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    live.set_kill_switch(user_id, True)
    await cycle(live, engine)
    assert engine.summary(user_id, bot["id"])["status"] == "halted"
    assert live.store.list_intents(user_id) == []


async def test_trailing_up_waits_keeps_holdings_and_raises_stop(grid, monkeypatch) -> None:
    live, engine, rules, user_id = grid
    bot = await engine.create(
        user_id,
        bot_values(
            trailing_enabled=True, trailing_trigger="0.01", trailing_confirm_seconds=60,
            trailing_max_recenters=2, trailing_ceiling="1.50",
        ),
    )
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    rules.price = Decimal("0.9400")
    await cycle(live, engine)
    rules.price = Decimal("1.2000")
    await cycle(live, engine)
    assert engine.summary(user_id, bot["id"])["status"] == "running"
    later = now_ms() + 61_000
    monkeypatch.setattr(live_bots_module, "now_ms", lambda: later)
    await cycle(live, engine)
    await cycle(live, engine)
    moved = engine.summary(user_id, bot["id"])
    assert moved["status"] == "running"
    assert moved["upper_price"] == "1.2500" and moved["lower_price"] == "1.0500"
    assert Decimal(moved["stop_price"]) > Decimal("0.85")
    assert moved["recenter_count"] == 1
    assert Decimal(moved["committed"]) <= Decimal("20")
    assert not buy_intents(live, user_id, "open")


async def test_unexpected_engine_error_halts_only_that_bot(grid, monkeypatch) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])

    async def explode(_bot):
        raise KeyError("boom")

    monkeypatch.setattr(engine, "tick", explode)
    await engine.run_cycle()
    halted = engine.summary(user_id, bot["id"])
    assert halted["status"] == "halted"
    assert "Unexpected engine error" in halted["halt_reason"]


async def test_delete_requires_stopped_bot(grid) -> None:
    live, engine, _, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    with pytest.raises(TradingError, match="Stop the bot"):
        engine.delete(user_id, bot["id"])
    engine.stop(user_id, bot["id"], liquidate=False)
    await cycle(live, engine)
    await cycle(live, engine)
    assert engine.summary(user_id, bot["id"])["status"] == "stopped"
    engine.delete(user_id, bot["id"])
    assert live.store.list_bots(user_id) == []


async def test_activity_lists_bot_orders_with_fill_times(grid) -> None:
    live, engine, rules, user_id = grid
    bot = await engine.create(user_id, bot_values())
    engine.start(user_id, bot["id"])
    await cycle(live, engine)
    before_fill = now_ms()
    rules.price = Decimal("0.9400")
    await cycle(live, engine)
    activity = engine.activity(user_id, bot["id"])
    assert activity["bot"]["id"] == bot["id"]
    assert Decimal(activity["fee_rate"]) >= 0
    orders = activity["orders"]
    assert orders and all(order["bot_id"] == bot["id"] for order in orders)
    filled = [order for order in orders if Decimal(order["executed_quantity"]) > 0]
    assert filled and all(order["filled_at"] >= before_fill for order in filled)
    assert all(order["filled_at"] is None for order in orders if order not in filled)
    with pytest.raises(TradingError):
        engine.activity(user_id, bot["id"] + 999)


def hourly_ranges(low: str, high: str, hours: int = 48) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(high), Decimal(low))] * hours


async def test_suggest_produces_a_valid_grid_from_recent_prices(grid) -> None:
    live, engine, _, user_id = grid
    result = await engine.suggest(user_id, "dry_run", "abc/usdt", hourly_ranges("0.90", "1.10"))
    values = result["values"]
    assert result["valid"], result["errors"]
    assert Decimal(values["stop_price"]) < Decimal(values["lower_price"]) < 1 < Decimal(values["upper_price"])
    assert Decimal(values["lower_price"]) == Decimal("0.9")
    assert Decimal(values["upper_price"]) == Decimal("1.1")
    assert MIN_LEVELS <= values["levels"] <= 20
    assert Decimal(values["order_value"]) <= Decimal("25")
    assert Decimal(values["max_allocation"]) <= Decimal("50")
    assert Decimal(values["trailing_ceiling"]) > Decimal(values["upper_price"])
    assert any("traded between" in note for note in result["notes"])
    bot = await engine.create(user_id, values)
    assert bot["status"] == "armed"


async def test_suggest_without_history_uses_a_band_around_price(grid) -> None:
    _, engine, _, user_id = grid
    result = await engine.suggest(user_id, "dry_run", "ABC/USDT", [])
    assert result["valid"], result["errors"]
    assert Decimal(result["values"]["lower_price"]) == Decimal("0.92")
    assert Decimal(result["values"]["upper_price"]) == Decimal("1.08")


async def test_suggest_keeps_price_inside_a_tight_recent_range(grid) -> None:
    _, engine, _, user_id = grid
    result = await engine.suggest(user_id, "dry_run", "ABC/USDT", hourly_ranges("0.999", "1.001"))
    values = result["values"]
    assert result["valid"], result["errors"]
    assert Decimal(values["lower_price"]) <= Decimal("0.97")
    assert Decimal(values["upper_price"]) >= Decimal("1.03")


async def test_suggest_reports_budget_too_small(grid) -> None:
    live, engine, _, user_id = grid
    live.update_limits(user_id, {"reserve_balance": "999.5"})
    result = await engine.suggest(user_id, "dry_run", "ABC/USDT", hourly_ranges("0.90", "1.10"))
    assert not result["valid"]
    assert any("budget" in error for error in result["errors"])


async def test_suggest_rejects_ineligible_market(grid) -> None:
    _, engine, _, user_id = grid
    with pytest.raises(TradingError):
        await engine.suggest(user_id, "dry_run", "ABC/BTC", [])
