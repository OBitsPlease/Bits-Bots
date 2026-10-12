from bitsbots.indicators import atr, ema_series, rsi, sma


def test_sma_requires_full_period() -> None:
    assert sma([1, 2], 3) is None
    assert sma([1, 2, 3], 3) == 2


def test_ema_tracks_rising_values() -> None:
    result = ema_series([1, 2, 3, 4, 5], 3)
    assert result[-1] > result[-2]
    assert result[-1] < 5


def test_rsi_handles_flat_and_rising_prices() -> None:
    assert rsi([5.0] * 20) == 50.0
    assert rsi([float(value) for value in range(20)]) == 100.0


def test_atr_includes_price_gaps() -> None:
    highs = [10, 11, 15, 16]
    lows = [9, 10, 14, 15]
    closes = [9.5, 10.5, 14.5, 15.5]
    assert atr(highs, lows, closes, 3) == (1.5 + 4.5 + 1.5) / 3

