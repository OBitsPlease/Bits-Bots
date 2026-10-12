"""Paper-only strategy challenger evaluation over stored candles.

This module deliberately depends only on paper bot configuration and public
stored candles.  It has no live-account or exchange-credential integration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .config import Settings
from .models import Candle, PaperBot
from .storage import Storage

DAY_MS = 86_400_000
MIN_TRADES = 15
MIN_DAYS = 7
MAX_CHALLENGERS = 3
MATERIAL_OBJECTIVE_EDGE = 0.01
MATERIAL_RETURN_EDGE = 0.005


@dataclass(frozen=True)
class Evaluation:
    started_at: int | None
    ended_at: int | None
    completed_trades: int
    net_return: float
    max_drawdown: float
    objective: float
    eligible: bool
    reason: str


def bot_settings(bot: PaperBot) -> dict[str, object]:
    return {
        "entry_score": bot.entry_score,
        "max_allocation": bot.max_allocation,
        "risk_level": bot.risk_level,
        "grid_slices": bot.grid_slices,
        "grid_stop_price": bot.grid_stop_price,
    }


def safe_variants(settings: dict[str, object]) -> list[dict[str, object]]:
    """Produce a small deterministic set that cannot increase bot risk."""
    threshold = float(settings["entry_score"])
    allocation = float(settings["max_allocation"])
    variants = []
    for threshold_delta, allocation_factor in ((2, 1.0), (5, 0.9), (-2, 0.8)):
        candidate = dict(settings)
        candidate["entry_score"] = min(95.0, max(50.0, threshold + threshold_delta))
        candidate["max_allocation"] = min(allocation, allocation * allocation_factor)
        variants.append(candidate)
    return variants[:MAX_CHALLENGERS]


def _rolling_score(history: list[Candle]) -> float:
    """A close-only score; callers pass candles strictly before the fill candle."""
    closes = [c.close for c in history]
    if len(closes) < 30:
        return 0.0
    short = sum(closes[-8:]) / 8
    long = sum(closes[-30:]) / 30
    momentum = (closes[-1] / closes[-8] - 1) * 400
    return max(0.0, min(100.0, 50 + (15 if short > long else -15) + momentum))


def simulate(
    candles: list[Candle], settings: dict[str, object], costs: Settings
) -> Evaluation:
    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    if len(ordered) < 32:
        return Evaluation(None, None, 0, 0.0, 0.0, 0.0, False, "insufficient candles")
    cash, quantity, entry = 1.0, 0.0, 0.0
    peak, drawdown, trades = 1.0, 0.0, 0
    threshold = float(settings["entry_score"])
    allocation = float(settings["max_allocation"])
    risk = {"low": 0.025, "medium": 0.04, "high": 0.06}.get(
        str(settings["risk_level"]), 0.04
    )
    # A decision using candle i is filled at candle i+1 open. This explicit
    # one-candle delay prevents the decision from seeing its own fill candle.
    for index in range(30, len(ordered) - 1):
        decision = ordered[index]
        fill = ordered[index + 1]
        score = _rolling_score(ordered[: index + 1])
        if quantity == 0 and score >= threshold:
            fill_price = fill.open * (1 + costs.slippage_rate)
            stake = min(cash, allocation)
            fee = stake * costs.fee_rate
            quantity = max(0.0, (stake - fee) / fill_price)
            cash -= stake
            entry = fill_price
        elif quantity and (
            score < max(38.0, threshold - 25)
            or decision.close <= entry * (1 - risk)
            or decision.close >= entry * (1 + risk * 2.5)
        ):
            fill_price = fill.open * (1 - costs.slippage_rate)
            proceeds = quantity * fill_price
            cash += proceeds - proceeds * costs.fee_rate
            quantity, entry = 0.0, 0.0
            trades += 1
        equity = cash + quantity * decision.close
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    if quantity:
        price = ordered[-1].close * (1 - costs.slippage_rate)
        proceeds = quantity * price
        cash += proceeds - proceeds * costs.fee_rate
        trades += 1
    elapsed = ordered[-1].timestamp - ordered[0].timestamp
    eligible = trades >= MIN_TRADES and elapsed >= MIN_DAYS * DAY_MS
    reason = "eligible" if eligible else (
        f"requires {MIN_TRADES} trades and {MIN_DAYS} days; "
        f"observed {trades} trades and {elapsed / DAY_MS:.1f} days"
    )
    net_return = cash - 1.0
    return Evaluation(
        ordered[0].timestamp, ordered[-1].timestamp, trades, net_return,
        drawdown, net_return - drawdown * 0.5, eligible, reason,
    )


class PaperLearner:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings

    def run_for_user(self, user_id: int) -> None:
        for bot in self.storage.list_bots(user_id):
            self._evaluate_bot(user_id, bot)

    def _evaluate_bot(self, user_id: int, bot: PaperBot) -> None:
        rows = self.storage.learner_versions(user_id, bot.strategy, bot.symbol)
        if not rows:
            # The global immutable baseline is also available as fallback
            # evidence when a market-specific scope has not matured.
            if not self.storage.learner_versions(user_id, bot.strategy, "*"):
                self.storage.create_learner_version(
                    user_id, bot.strategy, "*", "champion", bot_settings(bot)
                )
            champion_id = self.storage.create_learner_version(
                user_id, bot.strategy, bot.symbol, "champion", bot_settings(bot)
            )
            for variant in safe_variants(bot_settings(bot)):
                self.storage.create_learner_version(
                    user_id, bot.strategy, bot.symbol, "challenger",
                    variant, champion_id,
                )
            rows = self.storage.learner_versions(user_id, bot.strategy, bot.symbol)
        candles = self.storage.get_candles(bot.symbol, 60, limit=10_000)
        results: dict[int, Evaluation] = {}
        for row in rows:
            if row["role"] not in {"champion", "challenger"}:
                continue
            result = simulate(candles, json.loads(row["settings_json"]), self.settings)
            results[int(row["id"])] = result
            self.storage.record_learner_evaluation(
                user_id, int(row["id"]), bot.symbol, asdict(result)
            )
        champion = next((row for row in rows if row["role"] == "champion"), None)
        if champion is None:
            return
        base = results[int(champion["id"])]
        eligible = [
            row for row in rows
            if row["role"] == "challenger" and results[int(row["id"])].eligible
        ]
        if not base.eligible or not eligible:
            return
        winner = max(eligible, key=lambda row: results[int(row["id"])].objective)
        winning = results[int(winner["id"])]
        if (
            winning.objective < base.objective + MATERIAL_OBJECTIVE_EDGE
            or winning.net_return < base.net_return + MATERIAL_RETURN_EDGE
        ):
            return
        proposed = json.loads(winner["settings_json"])
        current = bot_settings(bot)
        if (
            proposed["risk_level"] != current["risk_level"]
            or float(proposed["max_allocation"]) > float(current["max_allocation"])
            or (
                current["grid_stop_price"] is not None
                and proposed["grid_stop_price"] is not None
                and float(proposed["grid_stop_price"]) < float(current["grid_stop_price"])
            )
        ):
            return
        reason = (
            f"material edge: objective {winning.objective:.4f} vs "
            f"{base.objective:.4f}; net {winning.net_return:.4f} vs "
            f"{base.net_return:.4f}"
        )
        self.storage.promote_learner_version(
            user_id, bot.strategy, bot.symbol, int(champion["id"]),
            int(winner["id"]), reason,
        )
        self.storage.update_bot_learned_settings(user_id, bot.id, proposed)
