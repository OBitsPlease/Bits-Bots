from dataclasses import asdict
import re
from typing import Any

from .models import PaperBot, PaperPosition, Signal

DISCLAIMER = (
    "This is a data-driven suggestion based on current and stored market data, "
    "not financial advice. Markets can change quickly and losses are possible."
)
STABLECOINS = {
    "BUSD",
    "DAI",
    "FDUSD",
    "GUSD",
    "PYUSD",
    "TUSD",
    "USDC",
    "USDD",
    "USDP",
    "USDS",
    "USDT",
}


def recommend_bot(
    signals: list[Signal],
    existing_bots: list[PaperBot],
    strategy: str = "Multi-TF",
) -> dict[str, Any]:
    configured_symbols = {bot.symbol for bot in existing_bots}
    unassigned = [
        signal for signal in signals if signal.symbol not in configured_symbols
    ]
    if not unassigned:
        raise ValueError("No unassigned scanned market is currently available")
    non_stable = [
        signal
        for signal in unassigned
        if signal.symbol.split("/", maxsplit=1)[0] not in STABLECOINS
    ]
    volatile = [
        signal
        for signal in non_stable
        if signal.price > 0 and signal.atr / signal.price >= 0.005
    ]
    candidates = volatile or non_stable or unassigned

    if strategy in {"Grid", "Adaptive Grid"}:
        candidate = max(
            candidates,
            key=lambda signal: (
                signal.history_quality == "good",
                -abs(signal.score - 62),
                signal.atr / signal.price if signal.price > 0 else 0,
            ),
        )
    else:
        candidate = max(candidates, key=lambda signal: signal.score)
    aligned_timeframes = sum(
        timeframe.score >= 60
        for timeframe in candidate.timeframe_scores
        if timeframe.candle_count >= 30
    )
    stronger_setup = (
        candidate.score >= 80
        and aligned_timeframes >= 3
        and candidate.history_quality == "good"
    )
    risk_level = "medium" if stronger_setup else "low"
    allocation = 0.08 if stronger_setup else 0.05
    entry_score = 72 if stronger_setup else 68
    base_asset = candidate.symbol.split("/", maxsplit=1)[0]
    recommendation: dict[str, Any] = {
        "name": (
            f"{base_asset} Adaptive Grid"
            if strategy == "Adaptive Grid"
            else f"{base_asset} Grid"
            if strategy == "Grid"
            else f"{base_asset} Momentum Scout"
        ),
        "symbol": candidate.symbol,
        "strategy": strategy,
        "risk_level": risk_level,
        "entry_score": entry_score if strategy == "Multi-TF" else 50,
        "max_allocation": allocation,
        "current_score": candidate.score,
        "history_quality": candidate.history_quality,
        "reasons": list(candidate.reasons[:4]),
        "disclaimer": DISCLAIMER,
    }
    if strategy in {"Grid", "Adaptive Grid"}:
        half_width = min(
            candidate.price * 0.20,
            max(candidate.atr * 4, candidate.price * 0.04),
        )
        lower_price = max(candidate.price - half_width, candidate.price * 0.01)
        upper_price = candidate.price + half_width
        grid_slices = 10 if strategy == "Adaptive Grid" else 8
        spacing = (upper_price - lower_price) / grid_slices
        recommendation.update(
            {
                "lower_price": lower_price,
                "upper_price": upper_price,
                "grid_slices": grid_slices,
                "grid_stop_price": max(
                    lower_price - max(candidate.atr * 1.5, spacing * 1.5),
                    candidate.price * 0.001,
                ),
            }
        )
    return recommendation


def answer_market_question(
    message: str,
    signals: list[Signal],
    bots: list[PaperBot],
    positions: list[PaperPosition],
    cash: float,
    equity: float,
) -> dict[str, Any]:
    if not message.strip():
        raise ValueError("Enter a question")
    if not signals:
        return {
            "answer": (
                "The scanner has not completed yet, so I do not have current "
                "market evidence to analyze. Wait for the scan and ask again."
            ),
            "context_symbol": None,
            "disclaimer": DISCLAIMER,
        }

    normalized = message.casefold()
    message_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    referenced = next(
        (
            signal
            for signal in signals
            if signal.symbol.casefold() in normalized
            or signal.symbol.split("/", maxsplit=1)[0].casefold() in message_tokens
        ),
        None,
    )
    top_signal = max(signals, key=lambda signal: signal.score)
    target = referenced or top_signal

    is_probability_question = any(
        phrase in normalized
        for phrase in (
            "odds",
            "chance",
            "chances",
            "probability",
            "likely",
            "likelihood",
            "guarantee",
            "guaranteed",
            "sure thing",
            "will it go up",
            "will it pump",
            "will it moon",
            "for sure",
            "will i profit",
            "will i make",
            "can i profit",
            "make a profit",
            "make money",
            "worth it",
            "should i buy",
            "should i invest",
            "good idea",
            "safe bet",
        )
    )

    if any(word in normalized for word in ("portfolio", "cash", "balance", "exposure")):
        open_positions = [position for position in positions if position.status == "open"]
        answer = (
            f"Your paper account currently has {cash:,.2f} USDT available and "
            f"estimated equity of {equity:,.2f} USDT, with "
            f"{len(open_positions)} open position(s). "
            "Check concentration, stops, and available cash before enabling "
            "another bot."
        )
        context_symbol = None
    elif is_probability_question:
        aligned_timeframes = sum(
            timeframe.score >= 60
            for timeframe in target.timeframe_scores
            if timeframe.candle_count >= 30
        )
        reasons = "; ".join(target.reasons[:3]) or "no strong signals recorded yet"
        if target.score >= 75 and target.history_quality == "good" and aligned_timeframes >= 3:
            read = (
                "Conditions currently lean favorable: the score is high, history "
                "quality is good, and multiple timeframes agree. That improves the "
                "odds relative to a random entry, but it is still not a guarantee."
            )
        elif target.score >= 60:
            read = (
                "Conditions are mixed-to-moderate right now. Some evidence supports "
                "an entry, but the setup is not strongly confirmed across "
                "timeframes, so treat any edge as modest."
            )
        else:
            read = (
                "Conditions are currently weak. The score is low and the evidence "
                "does not support a high-confidence entry right now; the odds of a "
                "quick profitable move look unfavorable based on current data."
            )
        answer = (
            "No bot, including this one, can quote a true probability of profit — "
            "markets are not fully predictable and past patterns do not guarantee "
            f"future moves. For {target.symbol}: current score is "
            f"{target.score:.0f}/100, history quality is {target.history_quality}, "
            f"and {aligned_timeframes} of {len(target.timeframe_scores)} tracked "
            f"timeframes currently agree. Supporting evidence: {reasons}. {read} "
            "Size any position so a loss is tolerable, since this is a screening "
            "signal, not a forecast."
        )
        context_symbol = target.symbol
    elif any(word in normalized for word in ("risk", "stop", "loss", "safe")):
        bot = next((item for item in bots if item.symbol == target.symbol), None)
        bot_context = (
            f"The configured bot uses {bot.risk_level} risk and caps allocation "
            f"at {bot.max_allocation:.0%}. "
            if bot
            else "There is no configured bot for this market. "
        )
        answer = (
            f"{target.symbol} has a current setup score of {target.score:.0f}/100 "
            f"and ATR of {target.atr:.8g}. {bot_context}"
            "A stop should reflect volatility and setup invalidation rather than "
            "a guaranteed loss limit; gaps and slippage can exceed it."
        )
        context_symbol = target.symbol
    elif any(
        phrase in normalized
        for phrase in ("best", "strongest", "opportunity", "what should", "candidate")
    ):
        reasons = "; ".join(top_signal.reasons[:3])
        answer = (
            f"The highest-ranked current setup is {top_signal.symbol} at "
            f"{top_signal.score:.0f}/100. Current supporting evidence: {reasons}. "
            f"History quality is {top_signal.history_quality}. This ranking is a "
            "screening result, not an instruction to enter."
        )
        context_symbol = top_signal.symbol
    elif any(word in normalized for word in ("bot", "automate", "configure")):
        configured = next((bot for bot in bots if bot.symbol == target.symbol), None)
        if configured:
            if configured.strategy == "Multi-TF":
                details = (
                    f"requires score {configured.entry_score:.0f}, uses "
                    f"{configured.risk_level} risk, and caps allocation at "
                    f"{configured.max_allocation:.0%}"
                )
            elif (
                configured.grid_slices is not None
                and configured.lower_price is not None
                and configured.upper_price is not None
                and configured.grid_stop_price is not None
            ):
                details = (
                    f"uses {configured.grid_slices} slices from "
                    f"{configured.lower_price:.8g} to "
                    f"{configured.upper_price:.8g}, with a stop at "
                    f"{configured.grid_stop_price:.8g} and total allocation "
                    f"capped at {configured.max_allocation:.0%}"
                )
            else:
                details = "has an incomplete grid configuration that needs attention"
            answer = (
                f"{configured.name} is a {configured.strategy} bot that is "
                f"{configured.status} on {configured.symbol}; it {details}."
            )
        else:
            answer = (
                f"{target.symbol} is not assigned to a bot. Its current score is "
                f"{target.score:.0f}/100. Use Auto-pick or Create Paper Bot to "
                "review settings before enabling simulated entries."
            )
        context_symbol = target.symbol
    else:
        timeframe_summary = ", ".join(
            f"{timeframe.period}m {timeframe.score:.0f}"
            for timeframe in target.timeframe_scores
        )
        reasons = "; ".join(target.reasons[:4])
        answer = (
            f"{target.symbol} is priced near {target.price:.8g} with a current "
            f"setup score of {target.score:.0f}/100. Timeframes: "
            f"{timeframe_summary}. Main observations: {reasons}."
        )
        context_symbol = target.symbol

    return {
        "answer": answer,
        "context_symbol": context_symbol,
        "signal": asdict(target) if context_symbol else None,
        "disclaimer": DISCLAIMER,
    }
