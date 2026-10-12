from dataclasses import replace
from pathlib import Path

import pytest

from bitsbots.config import SETTINGS
from bitsbots.models import Signal
from bitsbots.paper import PaperTrader
from bitsbots.storage import Storage


@pytest.fixture
def trader(tmp_path: Path) -> tuple[PaperTrader, Storage]:
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "test.db",
        starting_cash=10_000,
        fee_rate=0.002,
        slippage_rate=0.001,
    )
    storage = Storage(settings.database_path)
    user = storage.create_user("test@example.com", "Test User", "test-hash")
    yield PaperTrader(storage, settings, user.id), storage
    storage.close()


def signal(price: float, score: float = 80) -> Signal:
    return Signal(
        symbol="TEST/USDT",
        score=score,
        price=price,
        action="paper-buy" if score >= 68 else "avoid",
        reasons=("test setup",),
        timeframe_scores=(),
        atr=2,
        generated_at=1,
    )


def test_opens_only_one_position_per_symbol(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    paper.process([signal(100)])
    paper.process([signal(101)])
    assert len(storage.open_positions(paper.user_id)) == 1
    assert storage.cash(paper.user_id) < 10_000


def test_stop_closes_position_and_realizes_loss(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    paper.process([signal(100)])
    position = storage.open_positions(paper.user_id)[0]
    paper.process([signal(position.stop_price - 1, score=20)])
    closed = storage.all_positions(paper.user_id)[0]
    assert closed.status == "closed"
    assert closed.exit_reason == "stop"
    assert closed.realized_pnl is not None
    assert closed.realized_pnl < 0


def test_target_exit_does_not_reenter_in_the_same_processing_cycle(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    paper.process([signal(100)])
    position = storage.open_positions(paper.user_id)[0]

    events = paper.process([signal(position.target_price + 1, score=90)])

    assert len(events) == 1
    assert "target" in events[0]
    assert storage.open_positions(paper.user_id) == []


def test_empty_bot_configuration_blocks_new_entries(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    paper.process([signal(100)], entry_bots={})
    assert storage.open_positions(paper.user_id) == []


def test_bot_entry_threshold_controls_entries(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Threshold Bot",
        "TEST/USDT",
        "Multi-TF",
        "low",
        85,
        0.05,
    )
    paper.process([signal(100, score=80)], entry_bots={bot.symbol: bot})
    assert storage.open_positions(paper.user_id) == []
    paper.process([signal(100, score=90)], entry_bots={bot.symbol: bot})
    assert len(storage.open_positions(paper.user_id)) == 1


def test_grid_bot_opens_crossed_slice_and_closes_at_next_level(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Test Grid",
        "TEST/USDT",
        "Grid",
        "low",
        50,
        0.20,
        90,
        110,
        4,
        85,
    )
    paper.process([signal(100, score=50)], entry_bots={bot.symbol: bot})
    bot = storage.list_bots(paper.user_id)[0]
    assert bot.last_price == 100
    assert storage.open_positions(paper.user_id) == []

    paper.process([signal(94, score=30)], entry_bots={bot.symbol: bot})
    position = storage.open_positions(paper.user_id)[0]
    assert position.strategy == "Grid"
    assert position.grid_level == 95
    assert position.target_price == 100
    assert position.bot_id == bot.id

    bot = storage.list_bots(paper.user_id)[0]
    paper.process([signal(101, score=30)], entry_bots={bot.symbol: bot})
    closed = storage.all_positions(paper.user_id)[0]
    assert closed.status == "closed"
    assert closed.exit_reason == "grid-target"
    assert closed.realized_pnl is not None
    assert closed.realized_pnl > 0


def test_adaptive_grid_recenters_only_when_empty(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Adaptive Test",
        "TEST/USDT",
        "Adaptive Grid",
        "low",
        50,
        0.10,
        90,
        110,
        10,
        85,
    )
    paper.process([signal(100)], entry_bots={bot.symbol: bot})
    bot = storage.list_bots(paper.user_id)[0]
    events = paper.process([signal(120)], entry_bots={bot.symbol: bot})
    updated = storage.list_bots(paper.user_id)[0]
    assert events and "recentered" in events[0]
    assert updated.lower_price is not None and updated.lower_price < 120
    assert updated.upper_price is not None and updated.upper_price > 120
    assert updated.last_price == 120


def test_grid_does_not_open_slices_below_safety_stop(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Gap Safety Grid",
        "TEST/USDT",
        "Grid",
        "low",
        50,
        0.20,
        90,
        110,
        4,
        85,
    )
    paper.process([signal(100)], entry_bots={bot.symbol: bot})
    bot = storage.list_bots(paper.user_id)[0]
    events = paper.process([signal(80)], entry_bots={bot.symbol: bot})
    assert events == []
    assert storage.open_positions(paper.user_id) == []
    assert storage.list_bots(paper.user_id)[0].last_price == 80


def test_adaptive_grid_waits_one_scan_after_stop_before_recentering(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Adaptive Stop Grid",
        "TEST/USDT",
        "Adaptive Grid",
        "low",
        50,
        0.20,
        90,
        110,
        4,
        85,
    )
    paper.process([signal(100)], entry_bots={bot.symbol: bot})
    bot = storage.list_bots(paper.user_id)[0]
    paper.process([signal(94)], entry_bots={bot.symbol: bot})
    bot = storage.list_bots(paper.user_id)[0]
    events = paper.process([signal(80)], entry_bots={bot.symbol: bot})
    updated = storage.list_bots(paper.user_id)[0]
    assert any("grid-stop" in event for event in events)
    assert not any("recentered" in event for event in events)
    assert updated.lower_price == 90
    assert updated.upper_price == 110
    assert updated.last_price == 80


def test_resuming_grid_resets_stale_crossing_price(
    trader: tuple[PaperTrader, Storage],
) -> None:
    paper, storage = trader
    bot = storage.create_bot(
        paper.user_id,
        "Resume Grid",
        "TEST/USDT",
        "Grid",
        "low",
        50,
        0.10,
        90,
        110,
        4,
        85,
    )
    paper.process([signal(100)], entry_bots={bot.symbol: bot})
    assert storage.list_bots(paper.user_id)[0].last_price == 100
    storage.update_bot_status(paper.user_id, bot.id, "paused")
    resumed = storage.update_bot_status(paper.user_id, bot.id, "running")
    assert resumed is not None
    assert resumed.last_price is None
