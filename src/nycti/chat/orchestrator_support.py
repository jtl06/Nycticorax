from __future__ import annotations

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
TOOL_SYNTHESIS_EVIDENCE_CHAR_LIMIT = 9000
WEB_QUERY_OVERLAP_THRESHOLD = 0.72
WEB_QUERY_STOPWORDS = {
    "actual",
    "april",
    "beat",
    "earnings",
    "eps",
    "fy",
    "guidance",
    "latest",
    "may",
    "miss",
    "numbers",
    "q1",
    "report",
    "reported",
    "reports",
    "result",
    "results",
    "revenue",
}
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


def format_available_tool_guidance(
    *,
    available_tool_names: set[str],
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
        "\nFor company earnings, prefer official investor-relations earnings releases, SEC filings, or earnings-call "
        "transcripts. If search results are thin or third-party snippets disagree, search for the company investor "
        "relations release instead of guessing from snippets."
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
        f'<invoke name="{WEB_SEARCH_TOOL_NAME}">\n'
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


def build_evidence_followup_messages(
    *,
    base_messages: list[dict[str, object]],
    latest_tool_results: list[str],
) -> list[dict[str, object]]:
    return [
        *base_messages,
        {
            "role": "user",
            "content": (
                "Tool evidence so far:\n"
                + format_tool_evidence(latest_tool_results)
                + "\n\n"
                "Choose exactly one path:\n"
                "1. If this evidence is enough, output only the final Discord answer now.\n"
                "2. If this evidence is missing, stale, or wrong, call one of the available tools. "
                "Do not include prose when calling a tool.\n"
                "Keep final answers concise. Do not paste raw tool output or use markdown tables."
            ),
        },
    ]


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


def remember_web_query_keys(tool_calls: list[object], seen_web_query_keys: list[frozenset[str]]) -> None:
    for tool_call in tool_calls:
        for query_key in web_query_keys_from_tool_call(tool_call):
            if query_key:
                seen_web_query_keys.append(query_key)


def redundant_web_tool_calls(
    tool_calls: list[object],
    seen_web_query_keys: list[frozenset[str]],
) -> list[object]:
    if not seen_web_query_keys:
        return []
    redundant: list[object] = []
    for tool_call in tool_calls:
        if getattr(tool_call, "name", "") != WEB_SEARCH_TOOL_NAME:
            continue
        query_keys = web_query_keys_from_tool_call(tool_call)
        if query_keys and all(web_query_key_is_redundant(query_key, seen_web_query_keys) for query_key in query_keys):
            redundant.append(tool_call)
    return redundant


def web_query_keys_from_tool_call(tool_call: object) -> list[frozenset[str]]:
    if getattr(tool_call, "name", "") != WEB_SEARCH_TOOL_NAME:
        return []
    raw_arguments = str(getattr(tool_call, "arguments", "") or "").strip()
    try:
        payload = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    queries: list[str] = []
    raw_queries = payload.get("queries")
    if isinstance(raw_queries, list):
        queries.extend(str(query) for query in raw_queries)
    raw_query = payload.get("query")
    if isinstance(raw_query, str):
        queries.append(raw_query)
    return [web_query_key(query) for query in queries if query.strip()]


def web_query_key(query: str) -> frozenset[str]:
    tokens = [
        token
        for token in web_query_tokens(query)
        if token not in WEB_QUERY_STOPWORDS
    ]
    return frozenset(tokens or web_query_tokens(query))


def web_query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in query.casefold():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return [token for token in tokens if len(token) > 1]


def web_query_key_is_redundant(query_key: frozenset[str], seen_query_keys: list[frozenset[str]]) -> bool:
    if not query_key:
        return False
    for seen_key in seen_query_keys:
        if not seen_key:
            continue
        overlap = len(query_key & seen_key)
        denominator = max(min(len(query_key), len(seen_key)), 1)
        if overlap / denominator >= WEB_QUERY_OVERLAP_THRESHOLD:
            return True
    return False


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


def looks_like_tool_call_markup(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return (
        normalized.startswith("<|start|>")
        or "<|tool_calls_section_begin|>" in normalized
        or "<function_calls>" in normalized
        or "<|channel|>commentary to=" in normalized
    )


def elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
