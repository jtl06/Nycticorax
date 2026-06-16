from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.run_state import AgentOutputBudget
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from nycti.formatting import extract_think_content

if TYPE_CHECKING:
    from nycti.config import Settings
    from nycti.llm.client import LLMChatTurn

MIN_CHAT_REPLY_COMPLETION_TOKENS = 700
MIN_TOOL_FOLLOWUP_COMPLETION_TOKENS = 1400
MIN_FINAL_REPLY_COMPLETION_TOKENS = 2000
LENGTH_CONTINUATION_TOKEN_MARGIN = 0.92
ACTION_TOOL_NAMES = {
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
}


def collect_reasoning(turn: LLMChatTurn) -> list[str]:
    parts: list[str] = []
    if turn.reasoning_content:
        parts.append(turn.reasoning_content)
    parts.extend(extract_think_content(turn.raw_text))
    return parts


def tool_names(tools: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def format_available_tool_guidance(*, available_tool_names: set[str]) -> str:
    names = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    guidance = (
        "Available tools this turn:\n"
        f"- {names}\n"
        "Use only these native tools when they materially help. After tool results arrive, either answer or call a "
        "materially different tool request. Do not repeat an exact call or write textual/XML tool-call markup."
        "\nFor live or historical market comparisons, verify both current and reference values with tools."
        "\nFor volatile company-status facts such as IPOs, public/private status, listing status, ticker identity, "
        "market cap, and valuation, use current search or market tools instead of model memory."
        "\nFor combined public/private company valuations, combine current market data for public tickers with "
        "current source-backed valuation reports for private companies. Ignore crypto/token pages unless the user "
        "explicitly asks about a token."
        "\nTreat a current quote's symbol, company name, exchange, and timestamp as stronger evidence than model "
        "memory or older speculative web pages. Do not explain away a newly listed instrument as stale data."
        "\nFor earnings, prefer official investor-relations releases, SEC filings, or earnings-call transcripts."
        "\nNever construct an investor-relations URL. If an index omits the target link, search the exact release title."
        "\nUse browser_extract sparingly and only after normal url_extract fails on a JavaScript-heavy or blocked page."
        "\nUse the provided local date/time for freshness and relative dates."
    )
    action_tools = sorted(available_tool_names & ACTION_TOOL_NAMES)
    if action_tools:
        guidance += (
            "\nAction tools exposed this turn: "
            + ", ".join(action_tools)
            + ". Call them only when the user clearly requested that action."
        )
    return guidance


def format_inline_tool_fallback_guidance(
    *,
    available_tool_names: set[str],
    required_tool_names: set[str],
) -> str:
    required = ", ".join(sorted(required_tool_names)) if required_tool_names else "(none)"
    available = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    return (
        "Native tool schemas are unavailable. If a tool is required, output only XML tool-call markup:\n"
        "<function_calls>\n"
        f'<invoke name="{WEB_SEARCH_TOOL_NAME}">\n'
        '<parameter name="query">search terms</parameter>\n'
        "</invoke>\n"
        "</function_calls>\n"
        f"Required tools: {required}\n"
        f"Available tools: {available}"
    )


def increment_metric(metrics: dict[str, int | str] | None, key: str, amount: int = 1) -> None:
    if metrics is not None:
        metrics[key] = int(metrics.get(key, 0)) + amount


def tool_call_signature(name: str, arguments: str) -> str:
    normalized_arguments = arguments.strip()
    try:
        parsed = json.loads(normalized_arguments) if normalized_arguments else {}
    except json.JSONDecodeError:
        parsed = normalized_arguments
    return f"{name}:{json.dumps(parsed, sort_keys=True, separators=(',', ':'))}"


def chat_reply_max_tokens(settings: Settings) -> int:
    return max(settings.max_completion_tokens, MIN_CHAT_REPLY_COMPLETION_TOKENS)


def agent_output_budget(settings: Settings) -> AgentOutputBudget:
    reply_tokens = chat_reply_max_tokens(settings)
    return AgentOutputBudget(
        reply_tokens=reply_tokens,
        tool_followup_tokens=max(reply_tokens, MIN_TOOL_FOLLOWUP_COMPLETION_TOKENS),
        final_tokens=max(reply_tokens, MIN_FINAL_REPLY_COMPLETION_TOKENS),
        continuation_tokens=max(500, min(reply_tokens, 700)),
    )


def should_continue_answer(turn: LLMChatTurn, *, max_tokens: int) -> bool:
    if turn.finish_reason == "length":
        return True
    if max_tokens > 0 and turn.usage.completion_tokens >= int(max_tokens * LENGTH_CONTINUATION_TOKEN_MARGIN):
        return True
    return looks_structurally_incomplete_answer(turn.text)


def looks_structurally_incomplete_answer(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped.count("```") % 2 or stripped.count("**") % 2:
        return True
    if stripped.count("(") > stripped.count(")") or stripped.count("[") > stripped.count("]"):
        return True
    return stripped[-1] in {",", ";", ":", "-", "/", "(", "[", "*", "$"}


def join_continuation_parts(parts: list[str]) -> str:
    return "\n".join(part.strip() for part in parts if part.strip())


def first_result_line(result: str) -> str:
    for line in result.strip().splitlines():
        cleaned = line.strip()
        if cleaned:
            return truncate_text(cleaned, 120)
    return "(empty)"


def write_agent_trace(metrics: dict[str, int | str] | None, trace: AgentTrace) -> None:
    if metrics is None:
        return
    rendered = trace.render()
    if rendered:
        metrics["agent_trace"] = rendered


def append_raw_tool_trace(metrics: dict[str, int | str], raw_text: str) -> None:
    cleaned = raw_text.strip()
    if not cleaned or "<|tool_call" not in cleaned:
        return
    existing = str(metrics.get("raw_tool_trace", "")).strip()
    metrics["raw_tool_trace"] = existing + "\n\n---\n\n" + cleaned if existing else cleaned


def looks_like_raw_tavily_dump(text: str) -> bool:
    return text.strip().startswith(
        (
            "Tavily web results for:",
            "Tavily extract for:",
            "Tavily image results for:",
            "YouTube transcript for:",
        )
    )


def looks_like_tool_call_markup(text: str) -> bool:
    normalized = text.strip()
    return bool(
        normalized
        and (
            normalized.startswith("<|start|>")
            or "<|tool_calls_section_begin|>" in normalized
            or "<function_calls>" in normalized
            or "<|channel|>commentary to=" in normalized
        )
    )


def truncate_text(text: str, char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    return text[: max(char_limit - 20, 0)].rstrip() + "\n[truncated]"
