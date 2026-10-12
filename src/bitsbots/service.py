import asyncio
import sqlite3
import time
from dataclasses import asdict
from typing import Any

from .config import SETTINGS, Settings
from .models import Candle, Signal
from .learner import PaperLearner
from .nonkyc import NonKycClient, NonKycError
from .paper import PaperTrader
from .scanner import MarketScanner
from .storage import Storage


class TradingService:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        self.storage = Storage(settings.database_path)
        self.client = NonKycClient(settings)
        self.scanner = MarketScanner(
            self.client, self.storage, settings, self._set_status
        )
        self.learner = PaperLearner(self.storage, settings)
        self.signals: list[Signal] = []
        self.alerts: list[dict[str, Any]] = []
        self.paper_alerts: dict[int, list[dict[str, Any]]] = {}
        self.errors: list[str] = []
        self.status = "Starting"
        self.last_scan_at: int | None = None
        self._scan_lock = asyncio.Lock()
        self._candle_fetched_at: dict[tuple[str, int], float] = {}

    def _set_status(self, status: str) -> None:
        self.status = status

    async def close(self) -> None:
        await self.client.close()
        self.storage.close()

    async def available_markets(self) -> list[dict[str, Any]]:
        markets = await self.client.get_markets()
        available = [
            market
            for market in markets
            if market.active
            and market.last_price > 0
            and market.symbol.rsplit("/", maxsplit=1)[-1]
            in self.settings.quote_assets
        ]
        available.sort(key=lambda market: market.symbol)
        return [
            {
                "symbol": market.symbol,
                "last_price": market.last_price,
                "quote_volume": market.quote_volume,
                "last_trade_at": market.last_trade_at,
            }
            for market in available
        ]

    async def market_candles(
        self, symbol: str, period: int, limit: int = 100, max_age_ms: int | None = None
    ) -> list[Candle]:
        """Returns stored candles, refetching when the newest one is older than
        max_age_ms. Refetches are limited to one per market per minute so quiet
        markets with no new candles are not requested on every refresh."""
        normalized_symbol = symbol.strip().upper()
        stored = self.storage.get_candles(normalized_symbol, period, limit=limit)
        if stored and (
            max_age_ms is None or stored[-1].timestamp >= int(time.time() * 1000) - max_age_ms
        ):
            return stored
        key = (normalized_symbol, period)
        if stored and time.monotonic() - self._candle_fetched_at.get(key, float("-inf")) < 60:
            return stored
        try:
            fresh = await self.client.get_candles(normalized_symbol, period)
        except NonKycError:
            if stored:
                return stored
            raise
        self._candle_fetched_at[key] = time.monotonic()
        self.storage.save_candles(fresh)
        return self.storage.get_candles(normalized_symbol, period, limit=limit)

    async def scan_now(self) -> None:
        if self._scan_lock.locked():
            return
        async with self._scan_lock:
            self.status = "Scanning"
            try:
                priority_symbols = {
                    bot.symbol
                    for user_id in self.storage.list_user_ids()
                    for bot in self.storage.list_bots(user_id, running_only=True)
                }
                signals, errors = await self.scanner.scan(priority_symbols)
                now = int(time.time() * 1000)
                for user_id in self.storage.list_user_ids():
                    paper = PaperTrader(self.storage, self.settings, user_id)
                    bots = {
                        bot.symbol: bot
                        for bot in self.storage.list_bots(user_id, running_only=True)
                    }
                    paper_events = paper.process(signals, entry_bots=bots)
                    if paper_events:
                        current_alerts = self.paper_alerts.get(user_id, [])
                        self.paper_alerts[user_id] = (
                            [
                                {"message": event, "created_at": now}
                                for event in paper_events
                            ]
                            + current_alerts
                        )[:50]
                market_events: list[str] = []
                prior_actions = {
                    signal.symbol: signal.action for signal in self.signals
                }
                for signal in signals:
                    if (
                        signal.action == "paper-buy"
                        and prior_actions.get(signal.symbol) != "paper-buy"
                    ):
                        market_events.append(
                            f"{signal.symbol} entered the high-quality setup zone "
                            f"with score {signal.score:.0f}"
                        )
                self.alerts = (
                    [{"message": event, "created_at": now} for event in market_events]
                    + self.alerts
                )[:50]
                self.signals = signals
                self.errors = errors[:20]
                self.last_scan_at = now
                self.status = f"Watching {len(signals)} NonKYC trading pairs"
                # Paper learning is isolated from scan success/failure and runs
                # only after the scan and paper execution have completed.
                try:
                    await asyncio.to_thread(self._run_paper_learning)
                except (ValueError, sqlite3.Error) as exc:
                    self.errors = (
                        [f"Paper learner {type(exc).__name__}: {exc}"] + self.errors
                    )[:20]
            except (NonKycError, ValueError, sqlite3.Error) as exc:
                self.status = "Scan failed"
                self.errors = [f"{type(exc).__name__}: {exc}"]
                raise

    def _run_paper_learning(self) -> None:
        for user_id in self.storage.list_user_ids():
            self.learner.run_for_user(user_id)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.scan_now()
            except (NonKycError, ValueError, sqlite3.Error):
                pass
            await asyncio.sleep(self.settings.scan_interval_seconds)

    def snapshot(self, user_id: int) -> dict[str, Any]:
        prices = {signal.symbol: signal.price for signal in self.signals}
        paper = PaperTrader(self.storage, self.settings, user_id)
        positions = self.storage.all_positions(user_id)
        bots = self.storage.list_bots(user_id)
        cash = self.storage.cash(user_id)
        return {
            "status": self.status,
            "last_scan_at": self.last_scan_at,
            "signals": [asdict(signal) for signal in self.signals],
            "alerts": (self.paper_alerts.get(user_id, []) + self.alerts)[:50],
            "errors": self.errors,
            "account": {
                "cash": cash,
                "equity": paper.equity(prices),
                "starting_cash": self.settings.starting_cash,
            },
            "positions": [asdict(position) for position in positions],
            "bots": [asdict(bot) for bot in bots],
            "learning": self.storage.learner_snapshot(user_id),
            "settings": {
                "quote_asset": self.settings.quote_asset,
                "quote_assets": list(self.settings.quote_assets),
                "entry_score": self.settings.entry_score,
                "scan_interval_seconds": self.settings.scan_interval_seconds,
                "fee_rate": self.settings.fee_rate,
                "slippage_rate": self.settings.slippage_rate,
                "paper_only": True,
            },
        }
