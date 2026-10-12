import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .config import Settings
from .models import Candle, Market


class NonKycError(RuntimeError):
    """Raised when public NonKYC market data cannot be retrieved or parsed."""


class NonKycClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "BitsBots/0.1"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_markets(self) -> list[Market]:
        try:
            response = await self._http.get("/market/getlist")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NonKycError(f"Unable to retrieve NonKYC markets: {exc}") from exc
        if not isinstance(payload, list):
            raise NonKycError("NonKYC market list returned an unexpected payload")

        markets = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            try:
                last_price = float(item.get("lastPrice") or 0)
                base_volume = float(item.get("volume") or 0)
                markets.append(
                    Market(
                        symbol=str(item["symbol"]),
                        last_price=last_price,
                        high_price=float(item.get("highPrice") or last_price),
                        low_price=float(item.get("lowPrice") or last_price),
                        base_volume=base_volume,
                        quote_volume=base_volume * last_price,
                        last_trade_at=int(item.get("lastTradeAt") or 0),
                        active=bool(item.get("isActive", False)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return markets

    @asynccontextmanager
    async def _websocket(self) -> AsyncIterator[ClientConnection]:
        try:
            async with websockets.connect(
                self.settings.websocket_url,
                open_timeout=self.settings.websocket_timeout_seconds,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                yield websocket
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise NonKycError(f"NonKYC WebSocket connection failed: {exc}") from exc

    async def get_candles(self, symbol: str, period: int) -> list[Candle]:
        request = {
            "method": "subscribeCandles",
            "params": {
                "symbol": symbol,
                "period": period,
                "limit": self.settings.candle_limit,
            },
            "id": 1,
        }
        async with self._websocket() as websocket:
            await websocket.send(json.dumps(request))
            deadline = asyncio.get_running_loop().time() + self.settings.websocket_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                    message = json.loads(raw_message)
                except (asyncio.TimeoutError, json.JSONDecodeError) as exc:
                    raise NonKycError(
                        f"Timed out or received invalid candles for {symbol} {period}m"
                    ) from exc
                if message.get("error"):
                    raise NonKycError(
                        f"NonKYC rejected candles for {symbol} {period}m: {message['error']}"
                    )
                if message.get("method") != "snapshotCandles":
                    continue
                data = message.get("params", {}).get("data", [])
                return self._parse_candles(symbol, period, data)
        raise NonKycError(f"No candle snapshot returned for {symbol} {period}m")

    @staticmethod
    def _parse_candles(symbol: str, period: int, payload: Any) -> list[Candle]:
        if not isinstance(payload, list):
            raise NonKycError(f"Unexpected candle payload for {symbol} {period}m")
        candles: list[Candle] = []
        for item in payload:
            try:
                if isinstance(item, dict):
                    raw_timestamp = (
                        item.get("timestamp")
                        or item.get("time")
                        or item.get("t")
                        or item.get("datetime")
                    )
                    if isinstance(raw_timestamp, str) and not raw_timestamp.isdigit():
                        timestamp = int(
                            datetime.fromisoformat(
                                raw_timestamp.replace("Z", "+00:00")
                            ).timestamp()
                            * 1000
                        )
                    else:
                        timestamp = int(raw_timestamp)
                    open_price = float(item.get("open") or item.get("o"))
                    high = float(item.get("high") or item.get("max") or item.get("h"))
                    low = float(item.get("low") or item.get("min") or item.get("l"))
                    close = float(item.get("close") or item.get("c"))
                    volume = float(item.get("volume") or item.get("v") or 0)
                elif isinstance(item, list) and len(item) >= 6:
                    timestamp, open_price, high, low, close, volume = (
                        int(item[0]),
                        float(item[1]),
                        float(item[2]),
                        float(item[3]),
                        float(item[4]),
                        float(item[5]),
                    )
                else:
                    continue
                if timestamp < 10_000_000_000:
                    timestamp *= 1000
                candles.append(
                    Candle(
                        symbol=symbol,
                        period=period,
                        timestamp=timestamp,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                    )
                )
            except (TypeError, ValueError):
                continue
        candles.sort(key=lambda candle: candle.timestamp)
        if not candles:
            raise NonKycError(f"No valid candles returned for {symbol} {period}m")
        return candles
