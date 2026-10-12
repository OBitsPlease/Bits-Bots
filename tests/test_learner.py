from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bitsbots.config import SETTINGS
from bitsbots.learner import Evaluation, PaperLearner, safe_variants, simulate
from bitsbots.models import Candle
from bitsbots.storage import Storage


@pytest.fixture
def setup(tmp_path: Path):
    settings = replace(
        SETTINGS, database_path=tmp_path / "learner.db",
        fee_rate=0.002, slippage_rate=0.001,
    )
    storage = Storage(settings.database_path)
    user = storage.create_user("one@example.com", "One", "hash")
    bot = storage.create_bot(
        user.id, "Learner", "TEST/USDT", "Multi-TF", "medium", 68, 0.10
    )
    yield storage, settings, user, bot
    storage.close()


def candles(count: int = 240) -> list[Candle]:
    return [
        Candle(
            "TEST/USDT", 60, 1_700_000_000_000 + index * 3_600_000,
            100 + (index % 12), 102 + (index % 12), 99 + (index % 12),
            101 + (index % 12), 1000,
        )
        for index in range(count)
    ]


def test_no_promotion_before_trade_and_time_gates(setup) -> None:
    storage, settings, user, bot = setup
    storage.save_candles(candles(80))
    PaperLearner(storage, settings).run_for_user(user.id)
    snapshot = storage.learner_snapshot(user.id)
    assert snapshot["promotions"] == []
    assert all(
        version["eligible"] in (0, None)
        for version in snapshot["versions"]
        if version["market"] == bot.symbol
    )


def test_variants_never_raise_risk_or_allocation() -> None:
    source = {
        "entry_score": 68.0, "max_allocation": 0.10, "risk_level": "low",
        "grid_slices": None, "grid_stop_price": None,
    }
    variants = safe_variants(source)
    assert len(variants) <= 3
    assert all(item["risk_level"] == "low" for item in variants)
    assert all(float(item["max_allocation"]) <= 0.10 for item in variants)


def test_simulation_orders_candles_and_delays_decisions(monkeypatch) -> None:
    observed: list[list[int]] = []

    def score(history):
        observed.append([item.timestamp for item in history])
        return 0.0

    monkeypatch.setattr("bitsbots.learner._rolling_score", score)
    data = list(reversed(candles(40)))
    result = simulate(data, {
        "entry_score": 68, "max_allocation": 0.1, "risk_level": "low",
    }, SETTINGS)
    assert result.completed_trades == 0
    assert observed
    assert all(items == sorted(items) for items in observed)
    assert [len(items) for items in observed] == list(range(31, 40))


def test_material_challenger_promotes_and_updates_only_safe_fields(
    setup, monkeypatch
) -> None:
    storage, settings, user, bot = setup
    storage.save_candles(candles())

    def result(_candles, version, _settings):
        better = float(version["entry_score"]) == 70
        return Evaluation(
            1, 8 * 86_400_000, 15, 0.10 if better else 0.02,
            0.02, 0.09 if better else 0.01, True, "eligible",
        )

    monkeypatch.setattr("bitsbots.learner.simulate", result)
    PaperLearner(storage, settings).run_for_user(user.id)
    snapshot = storage.learner_snapshot(user.id)
    assert len(snapshot["promotions"]) == 1
    updated = storage.list_bots(user.id)[0]
    assert updated.entry_score == 70
    assert updated.risk_level == bot.risk_level
    assert updated.max_allocation <= bot.max_allocation


def test_learning_evidence_is_user_isolated(setup) -> None:
    storage, settings, user, _ = setup
    other = storage.create_user("two@example.com", "Two", "hash")
    storage.create_bot(
        other.id, "Other", "TEST/USDT", "Multi-TF", "low", 70, 0.05
    )
    storage.save_candles(candles(80))
    PaperLearner(storage, settings).run_for_user(user.id)
    assert storage.learner_snapshot(other.id)["versions"] == []
    assert storage.learner_snapshot(other.id)["promotions"] == []
