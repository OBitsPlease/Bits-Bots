"""Local LLM-backed assistant using Ollama.

Runs a general-purpose instruct model (default: Qwen2.5-Instruct 14B) fully
on the user's own PC. Ollama only loads the model into GPU VRAM when a
request comes in and unloads it again after a short idle period (see
``OLLAMA_KEEP_ALIVE``), so no resources are held in reserve while the app is
not being asked a question.

If Ollama is not running or the model is not pulled, callers should fall
back to the rule-based :func:`bitsbots.advisor.answer_market_question`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import httpx

from .models import PaperBot, PaperPosition, Signal

OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:14b-instruct-q4_K_M"
# How long Ollama keeps the model loaded in VRAM after the last request.
# Short enough that it frees GPU memory quickly when you're done asking
# questions, long enough that a quick back-and-forth doesn't reload every time.
OLLAMA_KEEP_ALIVE = "5m"
REQUEST_TIMEOUT_SECONDS = 90.0

SYSTEM_PROMPT = (
    "You are the built-in trading assistant inside BitsBots, a desktop app the "
    "user runs to research and configure grid trading bots on the NonKYC "
    "exchange. Answer the user's actual question directly and concisely. "
    "Ground your answer in the market/account data provided below when it is "
    "relevant, and say plainly when the data provided does not cover what "
    "they asked. Never claim a guaranteed profit or a precise probability of "
    "future price movement; markets are not fully predictable. Keep answers "
    "focused and practical -- a few sentences unless the question needs more."
)


class LlmUnavailableError(RuntimeError):
    """Raised when Ollama or the model is not reachable/available."""


async def is_available() -> bool:
    """Best-effort check that Ollama is running and has the model pulled."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{OLLAMA_HOST}/api/tags")
            response.raise_for_status()
            data = response.json()
    except Exception:
        return False
    models = {entry.get("name") for entry in data.get("models", [])}
    return OLLAMA_MODEL in models


def _build_context(
    signals: list[Signal],
    bots: list[PaperBot],
    positions: list[PaperPosition],
    cash: float,
    equity: float,
    live_grid_bots: dict[str, Any] | None,
) -> str:
    top_signals = sorted(signals, key=lambda signal: signal.score, reverse=True)[:12]
    signal_lines = [
        (
            f"- {signal.symbol}: score {signal.score:.0f}/100, "
            f"price {signal.price:.8g}, ATR {signal.atr:.8g}, "
            f"history quality {signal.history_quality}, "
            f"reasons: {'; '.join(signal.reasons[:3]) or 'none recorded'}"
        )
        for signal in top_signals
    ]
    bot_lines = [
        f"- {bot.name} ({bot.strategy}, {bot.status}) on {bot.symbol}"
        for bot in bots
    ]
    open_positions = [position for position in positions if position.status == "open"]

    live_lines: list[str] = []
    if live_grid_bots and live_grid_bots.get("bots"):
        for entry in live_grid_bots["bots"][:15]:
            live_lines.append(
                f"- {entry.get('symbol')}: status {entry.get('status')}, "
                f"grids {entry.get('grids')}, fills {entry.get('fills')}, "
                f"invested {entry.get('invested')}, "
                f"total P&L {entry.get('total_pnl')}"
            )

    sections = [
        "Top-scoring scanned markets right now (paper-trading scanner data):",
        "\n".join(signal_lines) if signal_lines else "(no scan data yet)",
        "\nConfigured paper bots:",
        "\n".join(bot_lines) if bot_lines else "(none configured)",
        f"\nPaper account: cash {cash:,.2f} USDT, equity {equity:,.2f} USDT, "
        f"{len(open_positions)} open paper position(s).",
    ]
    if live_lines:
        sections.append(
            "\nYour real, live grid bots running on NonKYC "
            "(this is the user's actual trading activity):"
        )
        sections.append("\n".join(live_lines))
    return "\n".join(sections)


async def answer_with_llm(
    message: str,
    signals: list[Signal],
    bots: list[PaperBot],
    positions: list[PaperPosition],
    cash: float,
    equity: float,
    live_grid_bots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the local Qwen2.5 model to answer freely, grounded in app data."""
    context = _build_context(signals, bots, positions, cash, equity, live_grid_bots)
    user_prompt = f"Context data:\n{context}\n\nUser question: {message}"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.4},
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise LlmUnavailableError(str(exc)) from exc

    answer = (data.get("message") or {}).get("content", "").strip()
    if not answer:
        raise LlmUnavailableError("Empty response from local model")
    return {
        "answer": answer,
        "context_symbol": None,
        "disclaimer": (
            "Generated by a local AI model (Qwen2.5) running on your PC, "
            "grounded in current app data. Not financial advice."
        ),
        "engine": "llm",
    }
