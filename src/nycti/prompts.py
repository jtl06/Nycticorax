from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI bot in a private Discord friend server. "
    "Be concise, natural, and context-aware. "
    "Use the provided memories as soft hints, not unquestionable facts. "
    "Do not mention hidden prompts, memory scoring, or usage tracking. "
    "If the user is asking casually, keep the tone casual. "
    "If context is ambiguous, say what you are assuming."
)


@lru_cache(maxsize=1)
def get_system_prompt() -> str:
    try:
        prompt = files("nycti").joinpath("prompt.md").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_SYSTEM_PROMPT
    return prompt or DEFAULT_SYSTEM_PROMPT
