from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    DEEP_RESEARCH_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, object]
    handler_name: str
    timeout_seconds: float
    budget_cost_units: int = 1
    fallback: str = "Explain the failed tool result briefly and answer from available context."

    def openai_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                "strict": True,
            },
        }


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    semantic_required = set(required)
    unknown_required = semantic_required - properties.keys()
    if unknown_required:
        raise ValueError(f"Unknown required properties: {sorted(unknown_required)}")

    # OpenAI strict function schemas require every property to appear in
    # `required`. Optional arguments are represented as nullable instead of
    # being omitted. Parsers still accept omitted fields for compatibility
    # with stored and non-strict tool calls.
    strict_properties = {
        name: value if name in semantic_required else _nullable_schema(value)
        for name, value in properties.items()
    }
    return {
        "type": "object",
        "properties": strict_properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable_schema(value: object) -> object:
    if not isinstance(value, dict):
        raise TypeError("Tool properties must be JSON Schema objects")
    schema = dict(value)
    value_type = schema.get("type")
    if isinstance(value_type, str):
        schema["type"] = [value_type, "null"]
    elif isinstance(value_type, list) and "null" not in value_type:
        schema["type"] = [*value_type, "null"]
    else:
        raise TypeError("Tool properties must declare a JSON Schema type")
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        schema["enum"] = [*enum, None]
    return schema


TOOL_SPECS: dict[str, ToolSpec] = {
    DEEP_RESEARCH_TOOL_NAME: ToolSpec(
        name=DEEP_RESEARCH_TOOL_NAME,
        description=(
            "Run bounded multi-query research with an economy model and return reduced, source-backed evidence "
            "for a rigorous answer. It can fan out across web search, exact URLs, live finance quotes, YouTube "
            "transcripts, and restricted calculations in one call. The specialized tools also remain available."
        ),
        parameters=_object_schema(
            {
                "question": {
                    "type": "string",
                    "description": "The complete self-contained research question.",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional scope, source preference, or comparison criteria.",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": "Optional exact public URLs to extract alongside web research.",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "description": "Optional market symbols whose live quotes should be included.",
                },
                "youtube_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 2,
                    "description": "Optional YouTube URLs whose transcripts should be included.",
                },
                "calculations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 2,
                    "description": (
                        "Optional restricted Python snippets for exact calculations; assign the result "
                        "to `result` or print it."
                    ),
                },
            },
            required=("question",),
        ),
        handler_name="_handle_deep_research",
        timeout_seconds=35,
        # One composite call can expand into several searches, extracts, and
        # economy-model reductions, so it must not consume the same budget as
        # a single cheap lookup.
        budget_cost_units=4,
        fallback=(
            "If composite research fails, use the other available read tools directly and clearly identify gaps."
        ),
    ),
    MEMORY_SEARCH_TOOL_NAME: ToolSpec(
        name=MEMORY_SEARCH_TOOL_NAME,
        description=(
            "Search Nycti's stored memories when background-prefetched context is incomplete. "
            "The server enforces private (requester only), guild_shared, and lore visibility; "
            "the model cannot expand access. This is read-only."
        ),
        parameters=_object_schema(
            {
                "query": {
                    "type": "string",
                    "description": "A focused semantic and lexical memory query.",
                },
                "owner_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                    "description": "Optional Discord user IDs to narrow owners, or null for all visible owners.",
                },
                "visibility_scopes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["private", "guild_shared", "lore"],
                    },
                    "maxItems": 3,
                    "description": "Optional visibility scopes to search, or null for all allowed scopes.",
                },
            },
            required=("query",),
        ),
        handler_name="_handle_memory_search",
        timeout_seconds=12,
        fallback="Continue from prefetched memory/context and do not infer private memories.",
    ),
    WEB_SEARCH_TOOL_NAME: ToolSpec(
        name=WEB_SEARCH_TOOL_NAME,
        description=(
            "Search fresh public web info. Batch up to 4 independent focused queries in one call. "
            "Use for current facts and dated reference facts; set time_range when recency matters."
        ),
        parameters=_object_schema(
            {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "One to four independent focused searches to run in parallel.",
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news", "finance"],
                    "description": "Search category, or null. Use news for changing public events.",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": (
                        "Freshness window, or null. Use null for historical facts or an explicit past date."
                    ),
                },
            },
            required=("queries",),
        ),
        handler_name="_handle_web_search",
        timeout_seconds=15,
        fallback="If search fails, say fresh web lookup failed and avoid guessing current facts.",
    ),
    STOCK_QUOTE_TOOL_NAME: ToolSpec(
        name=STOCK_QUOTE_TOOL_NAME,
        description=(
            "Fetch latest quotes for up to 10 stocks, ETFs, indexes, or futures, including available "
            "pre/post-market data when the regular market is closed."
        ),
        parameters=_object_schema(
            {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": "One to ten symbols to quote, such as AAPL, NVDA, SPY, SPX, or ES.",
                },
            },
            required=("symbols",),
        ),
        handler_name="_handle_stock_quote",
        timeout_seconds=15,
        fallback="If quote fails, explain the provider/symbol issue and avoid inventing prices.",
    ),
    PRICE_HISTORY_TOOL_NAME: ToolSpec(
        name=PRICE_HISTORY_TOOL_NAME,
        description="Fetch recent historical candles for one supported market symbol.",
        parameters=_object_schema(
            {
                "symbol": {"type": "string", "description": "One market symbol such as SPY, AAPL, or NVDA."},
                "interval": {"type": "string", "description": "Candle interval; defaults to 1day."},
                "outputsize": {
                    "type": "integer",
                    "description": "Number of candles from 1 to 30; defaults to 5.",
                },
                "start_date": {"type": "string", "description": "Optional inclusive start date or datetime."},
                "end_date": {"type": "string", "description": "Optional inclusive end date or datetime."},
            },
            required=("symbol",),
        ),
        handler_name="_handle_price_history",
        timeout_seconds=15,
        fallback="If history fails, explain that the symbol or provider lookup failed.",
    ),
    ANNUAL_PERFORMANCE_TOOL_NAME: ToolSpec(
        name=ANNUAL_PERFORMANCE_TOOL_NAME,
        description=(
            "Compute exact calendar-year underlying price changes and cash distributions for up to 5 market "
            "symbols from Yahoo Finance daily history. Use for annual dividend/distribution comparisons."
        ),
        parameters=_object_schema(
            {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "Symbols to compare, such as JEPI and SPX.",
                },
                "start_year": {
                    "type": "integer",
                    "minimum": 1970,
                    "maximum": 2100,
                    "description": "First calendar year; defaults to six years before the current year.",
                },
            },
            required=("symbols",),
        ),
        handler_name="_handle_annual_performance",
        timeout_seconds=15,
        fallback="If annual history fails, report the affected symbol and do not estimate missing values.",
    ),
    GET_CHANNEL_CONTEXT_TOOL_NAME: ToolSpec(
        name=GET_CHANNEL_CONTEXT_TOOL_NAME,
        description=(
            "Fetch older Discord context when the recent window is insufficient. "
            "Raw is smaller; summary reads a larger window."
        ),
        parameters=_object_schema(
            {
                "mode": {"type": "string", "enum": ["raw", "summary"]},
                "multiplier": {"type": "integer", "minimum": 1, "maximum": 3},
                "expand": {
                    "type": "boolean",
                    "description": "Use a wider per-message line cap when exact wording is needed.",
                },
            },
            required=("mode",),
        ),
        handler_name="_handle_channel_context",
        timeout_seconds=20,
        fallback="If context fetch fails, answer from recent context and mention the gap only if material.",
    ),
    IMAGE_SEARCH_TOOL_NAME: ToolSpec(
        name=IMAGE_SEARCH_TOOL_NAME,
        description="Search for direct image URLs when the user wants to see an example.",
        parameters=_object_schema(
            {"query": {"type": "string", "description": "The focused image search query."}},
            required=("query",),
        ),
        handler_name="_handle_image_search",
        timeout_seconds=15,
        fallback="If image search fails, answer text-only.",
    ),
    EXTRACT_URL_TOOL_NAME: ToolSpec(
        name=EXTRACT_URL_TOOL_NAME,
        description="Extract readable content from a specific public URL; optional query narrows focus.",
        parameters=_object_schema(
            {
                "url": {"type": "string", "description": "The exact public URL."},
                "query": {"type": "string", "description": "Optional extraction focus."},
            },
            required=("url",),
        ),
        handler_name="_handle_url_extract",
        timeout_seconds=20,
        fallback=(
            "If extraction is empty or the URL may be guessed, use web search to locate the exact source URL. "
            f"If the exact page is still thin or blocked, try {BROWSER_EXTRACT_TOOL_NAME} when configured."
        ),
    ),
    BROWSER_EXTRACT_TOOL_NAME: ToolSpec(
        name=BROWSER_EXTRACT_TOOL_NAME,
        description="Extract a JavaScript-heavy or blocked page with Chromium after normal extraction fails.",
        parameters=_object_schema(
            {
                "url": {"type": "string", "description": "The exact public URL."},
                "query": {"type": "string", "description": "Optional extraction focus."},
                "headed": {
                    "type": "boolean",
                    "description": "Use a headed browser only when explicitly needed and allowed.",
                },
            },
            required=("url",),
        ),
        handler_name="_handle_browser_extract",
        timeout_seconds=40,
        fallback="If browser extraction fails, summarize from available URL/search context.",
    ),
    YOUTUBE_TRANSCRIPT_TOOL_NAME: ToolSpec(
        name=YOUTUBE_TRANSCRIPT_TOOL_NAME,
        description="Extract and summarize a transcript from a specific YouTube video URL.",
        parameters=_object_schema(
            {
                "url": {"type": "string", "description": "The exact YouTube video URL."},
                "query": {"type": "string", "description": "Optional transcript focus."},
            },
            required=("url",),
        ),
        handler_name="_handle_youtube_transcript",
        timeout_seconds=30,
        fallback="If extraction fails, say the transcript was unavailable and do not infer its contents.",
    ),
    PYTHON_EXEC_TOOL_NAME: ToolSpec(
        name=PYTHON_EXEC_TOOL_NAME,
        description="Run a small calculation in a restricted Python sandbox without imports, files, or network.",
        parameters=_object_schema(
            {"code": {"type": "string", "description": "Assign the final value to result or print output."}},
            required=("code",),
        ),
        handler_name="_handle_python",
        timeout_seconds=8,
        fallback="If Python is disabled or rejected, answer without executing code.",
    ),
    CREATE_REMINDER_TOOL_NAME: ToolSpec(
        name=CREATE_REMINDER_TOOL_NAME,
        description=(
            "Propose an exact future reminder for the current user. This never creates the reminder directly; "
            "the user must confirm the validated proposal with /confirm."
        ),
        parameters=_object_schema(
            {
                "message": {"type": "string", "description": "The short reminder text."},
                "remind_at": {"type": "string", "description": "ISO 8601 local date or date-time."},
            },
            required=("message", "remind_at"),
        ),
        handler_name="_handle_create_reminder",
        timeout_seconds=10,
        fallback="Ask for a clearer future time if the reminder proposal is invalid or ambiguous.",
    ),
    SEND_CHANNEL_MESSAGE_TOOL_NAME: ToolSpec(
        name=SEND_CHANNEL_MESSAGE_TOOL_NAME,
        description=(
            "Propose an exact message to another channel. This never sends directly; the requesting user must "
            "confirm the validated target and content with /confirm."
        ),
        parameters=_object_schema(
            {
                "channel": {"type": "string", "description": "Known channel alias or numeric channel ID."},
                "message": {"type": "string", "description": "The message to send."},
            },
            required=("channel", "message"),
        ),
        handler_name="_handle_send_message",
        timeout_seconds=10,
        fallback="Do not create a proposal if the target channel or message content is unclear.",
    ),
}

def get_tool_spec(name: str) -> ToolSpec | None:
    return TOOL_SPECS.get(name)


def build_registered_tools(enabled_names: Collection[str] | None = None) -> list[dict[str, object]]:
    selected = set(enabled_names) if enabled_names is not None else None
    return [
        spec.openai_schema()
        for name, spec in TOOL_SPECS.items()
        if selected is None or name in selected
    ]
