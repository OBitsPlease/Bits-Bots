from bitsbots.models import Candle
from bitsbots.strategy import build_signal, score_timeframe


def candles(period: int, count: int = 100, rising: bool = True) -> list[Candle]:
    result = []
    for index in range(count):
        close = 100 + index if rising else 200 - index
        result.append(
            Candle(
                symbol="TEST/USDT",
                period=period,
                timestamp=index * period * 60_000,
                open=close - 0.5,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100 + index * 3,
            )
        )
    return result


def test_rising_timeframe_scores_above_falling_timeframe() -> None:
    rising = score_timeframe(60, candles(60, rising=True))
    falling = score_timeframe(60, candles(60, rising=False))
    assert rising.score > falling.score


def test_long_timeframe_downtrend_caps_signal() -> None:
    data = {
        1440: candles(1440, rising=False),
        240: candles(240, rising=True),
        60: candles(60, rising=True),
        15: candles(15, rising=True),
    }
    signal = build_signal("TEST/USDT", data)
    assert signal.score <= 45
    assert signal.action != "paper-buy"


def test_insufficient_history_is_explicit() -> None:
    score = score_timeframe(60, candles(60, count=10))
    assert score.score == 0
    assert "Insufficient" in score.reasons[0]

