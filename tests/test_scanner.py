from dataclasses import replace
from unittest.mock import Mock

from bitsbots.config import SETTINGS
from bitsbots.models import Market
from bitsbots.scanner import MarketScanner


def market(
    symbol: str,
    *,
    volume: float = 10_000,
    last_trade_at: int = 2_000_000_000_000,
) -> Market:
    return Market(
        symbol=symbol,
        last_price=1,
        high_price=1.1,
        low_price=0.9,
        base_volume=volume,
        quote_volume=volume,
        last_trade_at=last_trade_at,
        active=True,
    )


def scanner() -> MarketScanner:
    settings = replace(
        SETTINGS,
        minimum_quote_volume=50,
        stale_market_minutes=180,
        scan_market_limit=1,
    )
    return MarketScanner(Mock(), Mock(), settings)


def test_discovery_supports_usdt_and_usdc(monkeypatch) -> None:
    monkeypatch.setattr("bitsbots.scanner.time.time", lambda: 2_000_000_000)
    selected = scanner()._select_candidates(
        [market("BTC/USDT", volume=20_000), market("ETH/USDC", volume=10_000)],
        set(),
    )
    assert selected[0].symbol == "BTC/USDT"


def test_priority_bot_market_bypasses_discovery_liquidity_filter(
    monkeypatch,
) -> None:
    monkeypatch.setattr("bitsbots.scanner.time.time", lambda: 2_000_000_000)
    selected = scanner()._select_candidates(
        [
            market("BTC/USDT", volume=20_000),
            market("ALPHA/USDT", volume=1, last_trade_at=1),
        ],
        {"ALPHA/USDT"},
    )
    assert [item.symbol for item in selected] == ["ALPHA/USDT", "BTC/USDT"]


def test_unsupported_quote_is_not_selected(monkeypatch) -> None:
    monkeypatch.setattr("bitsbots.scanner.time.time", lambda: 2_000_000_000)
    selected = scanner()._select_candidates(
        [market("BTC/EUR", volume=100_000)],
        {"BTC/EUR"},
    )
    assert selected == []
