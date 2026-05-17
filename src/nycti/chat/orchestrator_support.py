from __future__ import annotations

from dataclasses import dataclass
import json
import time

from nycti.agent_trace import AgentTrace
from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)
from nycti.config import Settings
from nycti.formatting import extract_think_content
from nycti.llm.client import LLMChatTurn

MAX_LENGTH_CONTINUATION_ROUNDS = 2
MIN_CHAT_REPLY_COMPLETION_TOKENS = 700
MIN_TOOL_SYNTHESIS_COMPLETION_TOKENS = 220
TOOL_SYNTHESIS_TOKEN_DIVISOR = 4
LENGTH_CONTINUATION_TOKEN_MARGIN = 0.92
TOOL_PLANNER_CONTEXT_CHAR_LIMIT = 2800
TOOL_SYNTHESIS_EVIDENCE_CHAR_LIMIT = 9000
EVIDENCE_TOOL_NAMES = {
    BROWSER_EXTRACT_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
}
ACTION_TOOL_NAMES = {
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
}


@dataclass(frozen=True, slots=True)
class ChatToolPlan:
    need_tools: bool
    tools_to_try: tuple[str, ...]
    freshness_required: bool
    risk_level: str
    reason: str


def collect_reasoning(turn: LLMChatTurn) -> list[str]:
    parts: list[str] = []
    if turn.reasoning_content:
        parts.append(turn.reasoning_content)
    inline_think = extract_think_content(turn.raw_text)
    parts.extend(inline_think)
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


def latest_message_excerpt(messages: list[dict[str, object]], char_limit: int) -> str:
    if not messages:
        return ""
    content = messages[-1].get("content", "")
    if isinstance(content, str):
        text = content
    else:
        text = str(content)
    return truncate_text(text.strip(), char_limit)


def parse_tool_plan(text: str, available_tool_names: set[str]) -> ChatToolPlan | None:
    data = _load_json_object(text)
    if data is None:
        return None
    tools = _parse_tool_name_list(data.get("tools_to_try"), available_tool_names)
    need_tools = bool(data.get("need_tools"))
    if not need_tools:
        tools = []
    risk_level = str(data.get("risk_level", "low")).strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"
    reason = str(data.get("reason", "")).strip()
    return ChatToolPlan(
        need_tools=need_tools,
        tools_to_try=tuple(tools),
        freshness_required=bool(data.get("freshness_required")),
        risk_level=risk_level,
        reason=truncate_text(reason, 180),
    )


def _parse_tool_name_list(value: object, available_tool_names: set[str]) -> list[str]:
    tools: list[str] = []
    if not isinstance(value, list):
        return tools
    for item in value:
        if isinstance(item, str) and item in available_tool_names and item not in tools:
            tools.append(item)
    return tools


def _load_json_object(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    first_brace = stripped.find("{")
    if first_brace >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(stripped[first_brace:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def format_tool_plan_guidance(plan: ChatToolPlan) -> str:
    if plan.need_tools:
        tools = ", ".join(plan.tools_to_try) if plan.tools_to_try else "(model should choose from available tools)"
        guidance = (
            "Tool planning guidance:\n"
            f"- need_tools: true\n"
            f"- tools_to_try: {tools}\n"
            f"- freshness_required: {str(plan.freshness_required).lower()}\n"
            f"- risk_level: {plan.risk_level}\n"
        )
        if plan.reason:
            guidance += f"- reason: {plan.reason}\n"
        guidance += "Use this plan unless the current request or context clearly requires a different tool path."
        return guidance
    guidance = (
        "Tool planning guidance:\n"
        "- need_tools: false\n"
        f"- risk_level: {plan.risk_level}\n"
    )
    if plan.reason:
        guidance += f"- reason: {plan.reason}\n"
    guidance += "Answer without tools unless the request clearly depends on fresh facts, exact external content, or an action."
    return guidance


def format_available_tool_guidance(
    *,
    available_tool_names: set[str],
    plan: ChatToolPlan | None,
) -> str:
    names = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    guidance = (
        "Available tools this turn:\n"
        f"- {names}\n"
        "Use only these native tools if a tool is needed. Do not write textual or XML tool-call markup in the reply."
    )
    guidance += (
        "\nFor market questions that compare live data against any historical benchmark, record, prior close, "
        "or dated reference point, use tools to verify both sides of the comparison. Do not answer historical "
        "market records from model memory."
    )
    guidance += (
        "\nThe current local date/time is provided in the request context and is authoritative. "
        "When a factual answer depends on events, prices, records, releases, or historical facts that may be newer "
        "than model training or could have changed, use search or a domain tool to ground it."
    )
    action_tools = sorted(available_tool_names & ACTION_TOOL_NAMES)
    if action_tools:
        guidance += (
            "\nAction tools exposed this turn: "
            + ", ".join(action_tools)
            + ". Call them only when the user clearly requested that action."
        )
    if plan is not None and plan.need_tools:
        guidance += "\n\n" + format_tool_plan_guidance(plan)
    return guidance


def format_inline_tool_fallback_guidance(
    *,
    available_tool_names: set[str],
    required_tool_names: set[str],
) -> str:
    required = ", ".join(sorted(required_tool_names)) if required_tool_names else "(none)"
    available = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    return (
        "Native tool schemas are unavailable for this provider. "
        "If a tool is required, output only XML tool-call markup in this shape:\n"
        "<function_calls>\n"
        '<invoke name="web_search">\n'
        '<parameter name="query">search terms</parameter>\n'
        "</invoke>\n"
        "</function_calls>\n"
        f"Required tools before answering: {required}\n"
        f"Available tool names: {available}\n"
        "Do not answer in prose until required tools have been called."
    )


def format_tool_evidence(tool_results: list[str]) -> str:
    if not tool_results:
        return "(no tool evidence captured)"
    blocks: list[str] = []
    remaining = TOOL_SYNTHESIS_EVIDENCE_CHAR_LIMIT
    for index, result in enumerate(tool_results[-8:], start=1):
        cleaned = result.strip()
        if not cleaned:
            continue
        block = f"[{index}]\n{cleaned}"
        clipped = truncate_text(block, remaining)
        blocks.append(clipped)
        remaining -= len(clipped)
        if remaining <= 0:
            break
    return "\n\n".join(blocks) if blocks else "(no tool evidence captured)"


def tool_call_signature(name: str, arguments: str) -> str:
    normalized_arguments = arguments.strip()
    try:
        parsed = json.loads(normalized_arguments) if normalized_arguments else {}
    except json.JSONDecodeError:
        parsed = normalized_arguments
    return f"{name}:{json.dumps(parsed, sort_keys=True, separators=(',', ':'))}"


def truncate_text(text: str, char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    return text[: max(char_limit - 20, 0)].rstrip() + "\n[truncated]"


def chat_reply_max_tokens(settings: Settings) -> int:
    return max(settings.max_completion_tokens, MIN_CHAT_REPLY_COMPLETION_TOKENS)


def tool_synthesis_max_tokens(settings: Settings) -> int:
    return max(
        MIN_TOOL_SYNTHESIS_COMPLETION_TOKENS,
        chat_reply_max_tokens(settings) // TOOL_SYNTHESIS_TOKEN_DIVISOR,
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
    if stripped.count("```") % 2:
        return True
    if stripped.count("**") % 2:
        return True
    if stripped.count("(") > stripped.count(")"):
        return True
    if stripped.count("[") > stripped.count("]"):
        return True
    return stripped[-1] in {",", ";", ":", "-", "/", "(", "[", "*", "$"}


def join_continuation_parts(parts: list[str]) -> str:
    cleaned_parts = [part.strip() for part in parts if part.strip()]
    if not cleaned_parts:
        return ""
    return "\n".join(cleaned_parts)


def first_latency_metric(metrics: dict[str, int | str]) -> int:
    for key, value in metrics.items():
        if key.endswith("_ms") and isinstance(value, int):
            return value
    return 0


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
    if existing:
        metrics["raw_tool_trace"] = existing + "\n\n---\n\n" + cleaned
        return
    metrics["raw_tool_trace"] = cleaned


def looks_like_raw_tavily_dump(text: str) -> bool:
    normalized = text.strip()
    return normalized.startswith(
        (
            "Tavily web results for:",
            "Tavily extract for:",
            "Tavily image results for:",
            "YouTube transcript for:",
        )
    )


def elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
