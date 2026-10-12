from collections.abc import Sequence
from math import sqrt


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    if period <= 0 or not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if period <= 0 or len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    window = changes[-period:]
    average_gain = sum(max(change, 0.0) for change in window) / period
    average_loss = sum(max(-change, 0.0) for change in window) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    if period <= 0 or len(closes) <= period:
        return None
    true_ranges = []
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return sum(true_ranges[-period:]) / period


def standard_deviation(values: Sequence[float], period: int) -> float | None:
    mean = sma(values, period)
    if mean is None:
        return None
    variance = sum((value - mean) ** 2 for value in values[-period:]) / period
    return sqrt(variance)


def percent_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current - previous) / previous

