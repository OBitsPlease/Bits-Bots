import time

from .config import Settings
from .models import PaperBot, PaperPosition, Signal
from .storage import Storage


class PaperTrader:
    def __init__(self, storage: Storage, settings: Settings, user_id: int) -> None:
        self.storage = storage
        self.settings = settings
        self.user_id = user_id
        storage.initialize_account(user_id, settings.starting_cash)

    def process(
        self,
        signals: list[Signal],
        entry_bots: dict[str, PaperBot] | None = None,
    ) -> list[str]:
        events: list[str] = []
        by_symbol = {signal.symbol: signal for signal in signals}
        open_positions = self.storage.open_positions(self.user_id)
        stopped_grid_bot_ids: set[int] = set()
        exited_symbols: set[str] = set()

        for position in open_positions:
            signal = by_symbol.get(position.symbol)
            if signal is None:
                continue
            if (
                position.strategy in {"Grid", "Adaptive Grid"}
                and position.bot_id is not None
                and signal.price <= position.stop_price
            ):
                stopped_grid_bot_ids.add(position.bot_id)
            event = (
                self._update_grid_position(position, signal)
                if position.strategy in {"Grid", "Adaptive Grid"}
                else self._update_position(position, signal)
            )
            if event:
                events.append(event)
                exited_symbols.add(position.symbol)

        occupied_symbols = {
            position.symbol
            for position in self.storage.open_positions(self.user_id)
            if position.strategy == "Multi-TF"
        }
        for signal in sorted(signals, key=lambda item: item.score, reverse=True):
            if (
                len(
                    [
                        position
                        for position in self.storage.open_positions(self.user_id)
                        if position.strategy == "Multi-TF"
                    ]
                )
                >= self.settings.max_open_positions
            ):
                break
            bot = entry_bots.get(signal.symbol) if entry_bots is not None else None
            entry_allowed = (
                signal.action == "paper-buy"
                if entry_bots is None
                else (
                    bot is not None
                    and bot.strategy == "Multi-TF"
                    and signal.score >= bot.entry_score
                )
            )
            if (
                not entry_allowed
                or signal.symbol in occupied_symbols
                or signal.symbol in exited_symbols
            ):
                continue
            position = self._open_position(signal, bot)
            if position is not None:
                occupied_symbols.add(signal.symbol)
                events.append(
                    f"Paper bought {signal.symbol} at {position.entry_price:.8g}"
                )
        if entry_bots is not None:
            for bot in entry_bots.values():
                if bot.strategy not in {"Grid", "Adaptive Grid"}:
                    continue
                if bot.id in stopped_grid_bot_ids:
                    self.storage.update_bot_last_price(
                        self.user_id, bot.id, by_symbol[bot.symbol].price
                    )
                    continue
                signal = by_symbol.get(bot.symbol)
                if signal is not None:
                    events.extend(self._process_grid_bot(bot, signal))
        return events

    def _open_position(
        self, signal: Signal, bot: PaperBot | None = None
    ) -> PaperPosition | None:
        cash = self.storage.cash(self.user_id)
        equity = self.equity({signal.symbol: signal.price})
        entry_price = signal.price * (1 + self.settings.slippage_rate)
        stop_distance = max(signal.atr * 1.8, entry_price * 0.025)
        risk_rates = {"low": 0.0025, "medium": 0.005, "high": 0.01}
        risk_rate = risk_rates.get(bot.risk_level, self.settings.risk_per_trade) if bot else self.settings.risk_per_trade
        allocation_fraction = bot.max_allocation if bot else self.settings.max_position_fraction
        risk_budget = equity * risk_rate
        quantity_by_risk = risk_budget / stop_distance
        quantity_by_allocation = (
            equity * allocation_fraction
        ) / entry_price
        quantity_by_cash = cash / (entry_price * (1 + self.settings.fee_rate))
        quantity = min(quantity_by_risk, quantity_by_allocation, quantity_by_cash)
        if quantity <= 0:
            return None
        cost = quantity * entry_price
        fee = cost * self.settings.fee_rate
        self.storage.set_cash(self.user_id, cash - cost - fee)
        position = PaperPosition(
            id=None,
            symbol=signal.symbol,
            quantity=quantity,
            entry_price=entry_price,
            stop_price=entry_price - stop_distance,
            target_price=entry_price + stop_distance * 2.5,
            highest_price=entry_price,
            opened_at=signal.generated_at,
            entry_reasons=list(signal.reasons),
            bot_id=bot.id if bot else None,
            strategy=bot.strategy if bot else "Multi-TF",
        )
        position.id = self.storage.insert_position(self.user_id, position)
        return position

    def _update_position(
        self, position: PaperPosition, signal: Signal
    ) -> str | None:
        position.highest_price = max(position.highest_price, signal.price)
        initial_risk = max(position.entry_price - position.stop_price, 0.0)
        if position.highest_price >= position.entry_price + initial_risk:
            trailing_stop = position.highest_price - max(signal.atr * 2.0, initial_risk)
            position.stop_price = max(position.stop_price, trailing_stop)

        reason: str | None = None
        if signal.price <= position.stop_price:
            reason = "stop"
        elif signal.price >= position.target_price:
            reason = "target"
        elif signal.score < self.settings.exit_score:
            reason = "signal"

        if reason is None:
            self.storage.update_position(self.user_id, position)
            return None
        exit_price = signal.price * (1 - self.settings.slippage_rate)
        proceeds = position.quantity * exit_price
        fee = proceeds * self.settings.fee_rate
        position.status = "closed"
        position.closed_at = int(time.time() * 1000)
        position.exit_price = exit_price
        position.exit_reason = reason
        entry_cost = position.quantity * position.entry_price
        entry_fee = entry_cost * self.settings.fee_rate
        position.realized_pnl = proceeds - fee - entry_cost - entry_fee
        self.storage.set_cash(
            self.user_id, self.storage.cash(self.user_id) + proceeds - fee
        )
        self.storage.update_position(self.user_id, position)
        return (
            f"Paper sold {position.symbol} at {exit_price:.8g} "
            f"({reason}, P/L {position.realized_pnl:+.2f} USDT)"
        )

    def _process_grid_bot(self, bot: PaperBot, signal: Signal) -> list[str]:
        if (
            bot.lower_price is None
            or bot.upper_price is None
            or bot.grid_slices is None
            or bot.grid_stop_price is None
        ):
            return []
        lower_price = bot.lower_price
        upper_price = bot.upper_price
        grid_slices = bot.grid_slices
        open_levels = {
            position.grid_level
            for position in self.storage.open_positions(self.user_id)
            if position.bot_id == bot.id and position.grid_level is not None
        }
        if (
            bot.strategy == "Adaptive Grid"
            and not open_levels
            and not (lower_price <= signal.price <= upper_price)
        ):
            half_width = min(
                signal.price * 0.20,
                max(signal.atr * 4, signal.price * 0.04),
            )
            lower = max(signal.price - half_width, signal.price * 0.01)
            upper = signal.price + half_width
            spacing = (upper - lower) / grid_slices
            stop = max(
                lower - max(signal.atr * 1.5, spacing * 1.5),
                signal.price * 0.001,
            )
            self.storage.update_bot_grid_range(
                self.user_id, bot.id, lower, upper, stop, signal.price
            )
            return [
                f"Adaptive grid recentered {bot.symbol} to "
                f"{lower:.8g}-{upper:.8g}"
            ]

        previous_price = bot.last_price
        self.storage.update_bot_last_price(self.user_id, bot.id, signal.price)
        if (
            previous_price is None
            or signal.price >= previous_price
            or signal.price <= bot.grid_stop_price
        ):
            return []

        spacing = (upper_price - lower_price) / grid_slices
        levels = [
            lower_price + spacing * index
            for index in range(grid_slices)
        ]
        crossed_levels = [
            level
            for level in sorted(levels, reverse=True)
            if signal.price <= level < previous_price
            and not any(abs(level - open_level) < spacing / 1000 for open_level in open_levels)
        ]
        events: list[str] = []
        for level in crossed_levels:
            position = self._open_grid_position(signal, bot, level, spacing)
            if position is None:
                break
            open_levels.add(level)
            events.append(
                f"Grid bought {signal.symbol} near level {level:.8g} "
                f"at {position.entry_price:.8g}"
            )
        return events

    def _open_grid_position(
        self,
        signal: Signal,
        bot: PaperBot,
        level: float,
        spacing: float,
    ) -> PaperPosition | None:
        if bot.grid_slices is None or bot.grid_stop_price is None:
            return None
        cash = self.storage.cash(self.user_id)
        equity = self.equity({signal.symbol: signal.price})
        entry_price = signal.price * (1 + self.settings.slippage_rate)
        if entry_price <= bot.grid_stop_price:
            return None
        risk_rates = {"low": 0.0025, "medium": 0.005, "high": 0.01}
        risk_budget = equity * risk_rates.get(
            bot.risk_level, self.settings.risk_per_trade
        ) / bot.grid_slices
        stop_distance = max(entry_price - bot.grid_stop_price, entry_price * 0.001)
        quantity_by_risk = risk_budget / stop_distance
        quantity_by_allocation = (
            equity * bot.max_allocation / bot.grid_slices
        ) / entry_price
        quantity_by_cash = cash / (entry_price * (1 + self.settings.fee_rate))
        quantity = min(quantity_by_risk, quantity_by_allocation, quantity_by_cash)
        if quantity <= 0:
            return None
        cost = quantity * entry_price
        self.storage.set_cash(
            self.user_id,
            cash - cost - cost * self.settings.fee_rate,
        )
        position = PaperPosition(
            id=None,
            symbol=signal.symbol,
            quantity=quantity,
            entry_price=entry_price,
            stop_price=bot.grid_stop_price,
            target_price=level + spacing,
            highest_price=entry_price,
            opened_at=signal.generated_at,
            entry_reasons=[
                f"{bot.strategy} downward crossing at {level:.8g}",
                f"Grid target {level + spacing:.8g}",
            ],
            bot_id=bot.id,
            strategy=bot.strategy,
            grid_level=level,
        )
        position.id = self.storage.insert_position(self.user_id, position)
        return position

    def _update_grid_position(
        self, position: PaperPosition, signal: Signal
    ) -> str | None:
        position.highest_price = max(position.highest_price, signal.price)
        reason: str | None = None
        if signal.price <= position.stop_price:
            reason = "grid-stop"
        elif signal.price >= position.target_price:
            reason = "grid-target"
        if reason is None:
            self.storage.update_position(self.user_id, position)
            return None
        return self._close_position(position, signal, reason)

    def _close_position(
        self, position: PaperPosition, signal: Signal, reason: str
    ) -> str:
        exit_price = signal.price * (1 - self.settings.slippage_rate)
        proceeds = position.quantity * exit_price
        fee = proceeds * self.settings.fee_rate
        position.status = "closed"
        position.closed_at = int(time.time() * 1000)
        position.exit_price = exit_price
        position.exit_reason = reason
        entry_cost = position.quantity * position.entry_price
        entry_fee = entry_cost * self.settings.fee_rate
        position.realized_pnl = proceeds - fee - entry_cost - entry_fee
        self.storage.set_cash(
            self.user_id, self.storage.cash(self.user_id) + proceeds - fee
        )
        self.storage.update_position(self.user_id, position)
        return (
            f"Grid sold {position.symbol} at {exit_price:.8g} "
            f"({reason}, P/L {position.realized_pnl:+.2f} USDT)"
        )

    def equity(self, prices: dict[str, float]) -> float:
        position_value = sum(
            position.quantity * prices.get(position.symbol, position.entry_price)
            for position in self.storage.open_positions(self.user_id)
        )
        return self.storage.cash(self.user_id) + position_value
