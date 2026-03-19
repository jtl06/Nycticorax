from __future__ import annotations

import re
from typing import Mapping

SEARCH_TRIGGER_PHRASE = "use search"
SEC_TRIGGER_PHRASE = "use sec"


def format_ping_message(latency_seconds: float) -> str:
    latency_ms = round(max(latency_seconds, 0.0) * 1000)
    return f"Pong! `{latency_ms} ms`"


def format_latency_debug_block(metrics: Mapping[str, int | str]) -> str:
    ordered_keys = (
        "chat_model",
        "memory_model",
        "end_to_end_ms",
        "context_fetch_ms",
        "memory_retrieval_ms",
        "chat_llm_ms",
        "chat_usage_write_ms",
        "chat_commit_ms",
        "reply_generation_ms",
    )
    lines = ["latency_debug_ms"]
    for key in ordered_keys:
        if key in metrics:
            lines.append(f"{key}: {metrics[key]}")
    lines.append("memory_extraction: background")
    return "```text\n" + "\n".join(lines) + "\n```"


def append_debug_block(reply_text: str, debug_block: str, limit: int = 1900) -> str:
    suffix = "\n\n" + debug_block
    if len(reply_text) + len(suffix) <= limit:
        return reply_text + suffix
    trim_target = max(0, limit - len(suffix))
    if trim_target <= 3:
        return debug_block[:limit]
    trimmed = reply_text[: trim_target - 3].rstrip()
    return f"{trimmed}...{suffix}"


def strip_think_blocks(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def render_custom_emoji_aliases(text: str, replacements: Mapping[str, str]) -> str:
    if not replacements:
        return text

    def _replace(match: re.Match[str]) -> str:
        alias = match.group(1)
        return replacements.get(alias, match.group(0))

    return re.sub(r":([a-zA-Z0-9_]+):", _replace, text)


def extract_search_query(text: str) -> tuple[bool, str]:
    return _extract_trigger_query(text, SEARCH_TRIGGER_PHRASE)


def extract_sec_query(text: str) -> tuple[bool, str]:
    return _extract_trigger_query(text, SEC_TRIGGER_PHRASE)


def _extract_trigger_query(text: str, phrase: str) -> tuple[bool, str]:
    normalized = " ".join(text.split())
    if not normalized:
        return False, ""
    escaped = re.escape(phrase)
    match = re.search(rf"\b{escaped}\b", normalized, flags=re.IGNORECASE)
    if match is None:
        return False, normalized
    query = re.sub(rf"\b{escaped}\b", "", normalized, count=1, flags=re.IGNORECASE).strip()
    query = " ".join(query.split())
    return True, query
