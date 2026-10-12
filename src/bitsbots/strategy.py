import time

from .indicators import atr, ema_series, percent_change, rsi, sma, standard_deviation
from .models import Candle, Signal, TimeframeScore


TIMEFRAME_WEIGHTS = {1440: 0.30, 240: 0.30, 60: 0.30, 15: 0.10}


def score_timeframe(period: int, candles: list[Candle]) -> TimeframeScore:
    if len(candles) < 30:
        return TimeframeScore(period, 0.0, ("Insufficient candle history",), len(candles))

    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    fast_ema = ema_series(closes, 12)
    slow_ema = ema_series(closes, 26)
    current_rsi = rsi(closes, 14)
    volume_average = sma(volumes[:-1], min(20, len(volumes) - 1))
    volatility = standard_deviation(closes, 20)
    prior_volatility = standard_deviation(closes[:-10], 20)
    recent_high = max(candle.high for candle in candles[-21:-1])
    recent_low = min(candle.low for candle in candles[-21:-1])

    score = 50.0
    reasons: list[str] = []
    if fast_ema[-1] > slow_ema[-1]:
        score += 15
        reasons.append("Fast trend is above the slow trend")
    else:
        score -= 15
        reasons.append("Fast trend is below the slow trend")

    if fast_ema[-1] > fast_ema[-4]:
        score += 8
        reasons.append("Trend slope is rising")
    else:
        score -= 6

    if current_rsi is not None:
        if 50 <= current_rsi <= 68:
            score += 10
            reasons.append(f"Momentum is constructive (RSI {current_rsi:.0f})")
        elif current_rsi >= 78:
            score -= 12
            reasons.append(f"Momentum is overextended (RSI {current_rsi:.0f})")
        elif current_rsi < 40:
            score -= 10
            reasons.append(f"Momentum is weak (RSI {current_rsi:.0f})")

    if closes[-1] > recent_high:
        score += 12
        reasons.append("Price broke above its recent range")
    elif closes[-1] <= recent_low:
        score -= 12
        reasons.append("Price broke below its recent range")
    elif fast_ema[-1] > slow_ema[-1] and closes[-1] >= fast_ema[-1] * 0.98:
        score += 5
        reasons.append("Price is holding near trend support")

    if volume_average and volumes[-1] > volume_average * 1.5:
        score += 8
        reasons.append("Volume confirms the move")

    if (
        volatility is not None
        and prior_volatility is not None
        and prior_volatility > 0
        and volatility < prior_volatility * 0.75
    ):
        score += 5
        reasons.append("Volatility has compressed")

    if len(closes) >= 8 and percent_change(closes[-1], closes[-8]) > 0.20:
        score -= 8
        reasons.append("Recent advance is unusually steep")

    return TimeframeScore(period, max(0.0, min(100.0, score)), tuple(reasons), len(candles))


def build_signal(symbol: str, candles_by_period: dict[int, list[Candle]]) -> Signal:
    timeframe_scores = tuple(
        score_timeframe(period, candles_by_period.get(period, []))
        for period in TIMEFRAME_WEIGHTS
    )
    available = [
        score
        for score in timeframe_scores
        if score.candle_count >= 30 and TIMEFRAME_WEIGHTS[score.period] > 0
    ]
    if not available:
        raise ValueError(f"No usable candle history for {symbol}")
    total_weight = sum(TIMEFRAME_WEIGHTS[score.period] for score in available)
    weighted_score = sum(
        score.score * TIMEFRAME_WEIGHTS[score.period] for score in available
    ) / total_weight

    longer = {score.period: score.score for score in available if score.period >= 240}
    if longer and min(longer.values()) < 35:
        weighted_score = min(weighted_score, 45.0)

    preferred = candles_by_period.get(60) or next(iter(candles_by_period.values()))
    price = preferred[-1].close
    highs = [candle.high for candle in preferred]
    lows = [candle.low for candle in preferred]
    closes = [candle.close for candle in preferred]
    current_atr = atr(highs, lows, closes) or price * 0.03
    action = "watch"
    if weighted_score >= 68:
        action = "paper-buy"
    elif weighted_score < 38:
        action = "avoid"

    reasons = []
    for score in sorted(available, key=lambda item: item.period, reverse=True):
        label = {1440: "1D", 240: "4H", 60: "1H", 15: "15M"}[score.period]
        reasons.extend(f"{label}: {reason}" for reason in score.reasons[:3])

    minimum_count = min(score.candle_count for score in available)
    history_quality = "good" if minimum_count >= 200 else "limited"
    return Signal(
        symbol=symbol,
        score=round(weighted_score, 1),
        price=price,
        action=action,
        reasons=tuple(reasons),
        timeframe_scores=timeframe_scores,
        atr=current_atr,
        generated_at=int(time.time() * 1000),
        history_quality=history_quality,
    )

