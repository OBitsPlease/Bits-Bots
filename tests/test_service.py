from unittest.mock import AsyncMock, Mock

import pytest

from bitsbots.models import Candle
from bitsbots.service import TradingService


def candle(symbol: str = "BETA/USDT") -> Candle:
    return Candle(
        symbol=symbol,
        period=60,
        timestamp=1_700_000_000_000,
        open=0.5,
        high=0.6,
        low=0.4,
        close=0.55,
        volume=100,
    )


@pytest.mark.asyncio
async def test_market_candles_returns_cached_history_without_network() -> None:
    service = TradingService.__new__(TradingService)
    cached = [candle()]
    service.storage = Mock()
    service.storage.get_candles.return_value = cached
    service.client = Mock()
    service.client.get_candles = AsyncMock()

    result = await service.market_candles("beta/usdt", 60)

    assert result == cached
    service.storage.get_candles.assert_called_once_with(
        "BETA/USDT", 60, limit=100
    )
    service.client.get_candles.assert_not_awaited()


@pytest.mark.asyncio
async def test_market_candles_fetches_and_stores_unscanned_market() -> None:
    service = TradingService.__new__(TradingService)
    fresh = [candle()]
    service.storage = Mock()
    service.storage.get_candles.side_effect = [[], fresh]
    service._candle_fetched_at = {}
    service.client = Mock()
    service.client.get_candles = AsyncMock(return_value=fresh)

    result = await service.market_candles("beta/usdt", 60)

    assert result == fresh
    service.client.get_candles.assert_awaited_once_with("BETA/USDT", 60)
    service.storage.save_candles.assert_called_once_with(fresh)


@pytest.mark.asyncio
async def test_market_candles_refetches_stale_history_once_per_minute() -> None:
    from bitsbots.nonkyc import NonKycError

    service = TradingService.__new__(TradingService)
    stale = [candle()]
    fresh = [Candle(**{**candle().__dict__, "timestamp": 1_700_000_060_000})]
    service.storage = Mock()
    service.storage.get_candles.side_effect = [stale, fresh, stale, stale]
    service._candle_fetched_at = {}
    service.client = Mock()
    service.client.get_candles = AsyncMock(return_value=fresh)

    assert await service.market_candles("beta/usdt", 60, max_age_ms=3_600_000) == fresh
    service.client.get_candles.assert_awaited_once_with("BETA/USDT", 60)
    assert await service.market_candles("beta/usdt", 60, max_age_ms=3_600_000) == stale
    service.client.get_candles.assert_awaited_once()

    service._candle_fetched_at = {}
    service.client.get_candles = AsyncMock(side_effect=NonKycError("down"))
    assert await service.market_candles("beta/usdt", 60, max_age_ms=3_600_000) == stale
