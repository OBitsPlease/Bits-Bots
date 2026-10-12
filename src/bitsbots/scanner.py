import asyncio
import time
from collections.abc import Callable

from .config import SUPPORTED_PERIODS, Settings
from .models import Market, Signal
from .nonkyc import NonKycClient, NonKycError
from .storage import Storage
from .strategy import build_signal


class MarketScanner:
    def __init__(
        self,
        client: NonKycClient,
        storage: Storage,
        settings: Settings,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.settings = settings
        self.on_status = on_status or (lambda _: None)

    async def scan(
        self, priority_symbols: set[str] | None = None
    ) -> tuple[list[Signal], list[str]]:
        markets = await self.client.get_markets()
        candidates = self._select_candidates(markets, priority_symbols or set())
        signals: list[Signal] = []
        errors: list[str] = []
        semaphore = asyncio.Semaphore(4)

        async def scan_market(market: Market) -> None:
            async with semaphore:
                try:
                    signal = await self._scan_market(market)
                    signals.append(signal)
                    self.storage.save_signal(signal)
                except (NonKycError, ValueError) as exc:
                    errors.append(str(exc))

        self.on_status(
            f"Scanning {len(candidates)} liquid {self.settings.quote_asset} "
            "pairs on NonKYC"
        )
        await asyncio.gather(*(scan_market(market) for market in candidates))
        signals.sort(key=lambda signal: signal.score, reverse=True)
        return signals, errors

    def _select_candidates(
        self, markets: list[Market], priority_symbols: set[str]
    ) -> list[Market]:
        now_ms = int(time.time() * 1000)
        maximum_age = self.settings.stale_market_minutes * 60 * 1000
        supported = [
            market
            for market in markets
            if market.active
            and market.symbol.rsplit("/", maxsplit=1)[-1]
            in self.settings.quote_assets
            and market.last_price > 0
        ]
        eligible = [
            market
            for market in supported
            if market.quote_volume >= self.settings.minimum_quote_volume
            and market.last_trade_at > 0
            and now_ms - market.last_trade_at <= maximum_age
        ]
        ranked = sorted(
            eligible, key=lambda market: market.quote_volume, reverse=True
        )
        priority = [
            market for market in supported if market.symbol in priority_symbols
        ]
        priority_set = {market.symbol for market in priority}
        discovery = [
            market for market in ranked if market.symbol not in priority_set
        ][: self.settings.scan_market_limit]
        return priority + discovery

    async def _scan_market(self, market: Market) -> Signal:
        candles_by_period = {}
        for period in SUPPORTED_PERIODS:
            fresh = await self.client.get_candles(market.symbol, period)
            self.storage.save_candles(fresh)
            candles_by_period[period] = self.storage.get_candles(
                market.symbol, period, limit=1000
            )
        return build_signal(market.symbol, candles_by_period)
