import pytest

from bitsbots.advisor import DISCLAIMER, answer_market_question, recommend_bot
from bitsbots.models import PaperBot, Signal, TimeframeScore


def signal(symbol: str, score: float, history_quality: str = "limited") -> Signal:
    return Signal(
        symbol=symbol,
        score=score,
        price=100,
        action="paper-buy" if score >= 68 else "watch",
        reasons=(
            "1D: Fast trend is above the slow trend",
            "4H: Momentum is constructive",
            "1H: Volume confirms the move",
        ),
        timeframe_scores=tuple(
            TimeframeScore(period, score, (), 250)
            for period in (1440, 240, 60, 15)
        ),
        atr=3,
        generated_at=1,
        history_quality=history_quality,
    )


def bot(symbol: str) -> PaperBot:
    return PaperBot(
        id=1,
        name="Existing",
        symbol=symbol,
        strategy="Multi-TF",
        risk_level="medium",
        status="running",
        entry_score=68,
        max_allocation=0.1,
        created_at=1,
    )


def test_recommendation_chooses_best_unassigned_market() -> None:
    recommendation = recommend_bot(
        [signal("BTC/USDT", 92, "good"), signal("ETH/USDT", 84, "good")],
        [bot("BTC/USDT")],
    )
    assert recommendation["symbol"] == "ETH/USDT"
    assert recommendation["risk_level"] == "medium"
    assert recommendation["max_allocation"] <= 0.1
    assert recommendation["disclaimer"] == DISCLAIMER


def test_recommendation_uses_conservative_defaults_for_limited_history() -> None:
    recommendation = recommend_bot([signal("TEST/USDT", 78)], [])
    assert recommendation["risk_level"] == "low"
    assert recommendation["max_allocation"] == 0.05
    assert recommendation["entry_score"] == 68


def test_grid_recommendation_includes_fee_aware_range_controls() -> None:
    recommendation = recommend_bot(
        [signal("ETH/USDT", 62, "good")],
        [],
        "Grid",
    )
    assert recommendation["strategy"] == "Grid"
    assert recommendation["lower_price"] < 100 < recommendation["upper_price"]
    assert recommendation["grid_stop_price"] < recommendation["lower_price"]
    assert recommendation["grid_slices"] == 8


def test_adaptive_grid_recommendation_uses_more_slices() -> None:
    recommendation = recommend_bot(
        [signal("ETH/USDT", 62, "good")],
        [],
        "Adaptive Grid",
    )
    assert recommendation["strategy"] == "Adaptive Grid"
    assert recommendation["grid_slices"] == 10


def test_recommendation_avoids_stablecoin_momentum_pairs() -> None:
    recommendation = recommend_bot(
        [signal("USDC/USDT", 99), signal("ETH/USDT", 80)],
        [],
    )
    assert recommendation["symbol"] == "ETH/USDT"


def test_recommendation_requires_an_unassigned_market() -> None:
    with pytest.raises(ValueError, match="No unassigned"):
        recommend_bot([signal("BTC/USDT", 90)], [bot("BTC/USDT")])


def test_assistant_grounds_symbol_answer_in_current_signal() -> None:
    response = answer_market_question(
        "What signs do you see for ETH?",
        [signal("BTC/USDT", 92), signal("ETH/USDT", 81)],
        [],
        [],
        cash=10_000,
        equity=10_000,
    )
    assert response["context_symbol"] == "ETH/USDT"
    assert "81/100" in response["answer"]
    assert response["disclaimer"] == DISCLAIMER


def test_assistant_answers_profit_odds_question_instead_of_bot_status() -> None:
    response = answer_market_question(
        "what are the odds i can set up a trading bot for ALPHA and make a profit?",
        [signal("ALPHA/USDT", 68, "good")],
        [bot("ALPHA/USDT")],
        [],
        cash=10_000,
        equity=10_000,
    )
    assert response["context_symbol"] == "ALPHA/USDT"
    lowered = response["answer"].casefold()
    assert "no bot" in lowered
    assert "guarantee" in lowered
    assert "68/100" in response["answer"]
    # Must not fall back to the generic bot-configuration recital.
    assert "is a multi-tf bot that is" not in lowered


def test_assistant_answers_profit_chance_question_for_unconfigured_symbol() -> None:
    response = answer_market_question(
        "what are the chances for profit if i create a trading bot for YEC?",
        [signal("YEC/USDT", 45, "limited")],
        [],
        [],
        cash=10_000,
        equity=10_000,
    )
    assert response["context_symbol"] == "YEC/USDT"
    lowered = response["answer"].casefold()
    assert "no bot" in lowered
    assert "unfavorable" in lowered


def test_assistant_reports_paper_portfolio_context() -> None:
    response = answer_market_question(
        "Summarize my portfolio",
        [signal("BTC/USDT", 92)],
        [],
        [],
        cash=8_500,
        equity=10_250,
    )
    assert "8,500.00 USDT" in response["answer"]
    assert "10,250.00 USDT" in response["answer"]
    assert response["context_symbol"] is None
