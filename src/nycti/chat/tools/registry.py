from __future__ import annotations

from collections.abc import Collection, Sequence
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
    REPORT_RESPONSE_ISSUE_TOOL_NAME,
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
    min_work_seconds_to_start: float = 0.0
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
            "High-latency, high-cost meta-tool for genuine multi-source synthesis or a request that deliberately "
            "combines several capabilities. It can fan out across web search, exact URLs, live finance quotes, "
            "YouTube transcripts, and restricted calculations with an economy model. It cannot pass symbols "
            "discovered during internal web searches into live finance in the same call. For a single current fact, "
            "news item, price, sector or dynamic-universe screen, URL, transcript, or calculation, use the matching "
            "direct tool first and escalate only for a concrete research gap. Copy every explicit specialized input "
            "into its matching field; leaving an input only in `question` does not run that capability. One successful "
            "call already performs its own search, extraction, and reduction; use another read tool only for a "
            "concrete missing requirement. For a terse mixed request such as `XYZ + 2*3 + page URL + YouTube "
            "URL`, use symbols=[`XYZ`], calculations=[`result = 2*3`], urls=[page URL], and "
            "youtube_urls=[YouTube URL] in that same call."
        ),
        parameters=_object_schema(
            {
                "question": {
                    "type": "string",
                    "description": (
                        "Restate the user's complete self-contained research request, including every subject and "
                        "requested output; never pass only a depth prefix such as `Deep`."
                    ),
                },
                "focus": {
                    "type": "string",
                    "description": "Optional scope, source preference, or comparison criteria.",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": (
                        "Every non-YouTube public page URL explicitly supplied by the user, copied exactly. "
                        "Use null only when no such page URL was supplied; put YouTube URLs in `youtube_urls`."
                    ),
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 10,
                    "description": (
                        "Every explicitly supplied or confidently identified market symbol whose live quote is "
                        "needed. Copy ticker-form inputs here, without a leading `$`; do not place calculations "
                        "or ordinary words here. Use null when none are present."
                    ),
                },
                "youtube_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 2,
                    "description": (
                        "Every YouTube URL explicitly supplied by the user, copied exactly. Use null only when "
                        "none was supplied; do not also place these URLs in `urls`."
                    ),
                },
                "calculations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 2,
                    "description": (
                        "Every explicit calculation requested by the user as a restricted Python snippet; "
                        "preserve the expression and assign the result to `result` or print it. Use null only "
                        "when no calculation was supplied."
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
        min_work_seconds_to_start=25.0,
        fallback=(
            "If arguments were invalid, correct the exact specialized fields and retry this meta-tool once. "
            "If nested research actually fails, use direct read tools only for the missing requirements and "
            "clearly identify gaps."
        ),
    ),
    MEMORY_SEARCH_TOOL_NAME: ToolSpec(
        name=MEMORY_SEARCH_TOOL_NAME,
        description=(
            "Search Nycti's stored memories only when the request depends on user-specific, guild-specific, "
            "lore, or prior-conversation facts and background-prefetched context is incomplete. Do not use "
            "memory as a fallback for public facts, product/version knowledge, or web research. "
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
                "country": {
                    "type": "string",
                    "description": (
                        "Optional English country name to boost local sources, such as china. Available only with "
                        "topic=general. Write the query itself in the requested local language."
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
            "Fetch latest quotes for up to 10 stocks, ETFs, indexes, futures, or FX pairs, including available "
            "pre/post-market data when the regular market is closed. Public-company results also include current "
            "market cap and shares outstanding when Yahoo exposes them, so use one batched call for market-cap "
            "comparisons and price-to-match-valuation calculations instead of searching headlines. "
            "If the user supplies ticker-form symbols, "
            "call this directly even when a symbol is unfamiliar. Batch every known requested symbol into one "
            "call. Pass currency pairs as BASE/QUOTE, such as USD/JPY; common Yahoo =X aliases are accepted too. "
            "For a current sector or universe screen, use web once when needed to identify symbols, then batch "
            "a representative benchmark plus representative or named constituents here. Use that breadth with "
            "current news before attributing a group move to one catalyst; deep research does not replace live "
            "quote coverage."
        ),
        parameters=_object_schema(
            {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": (
                        "One to ten symbols or FX pairs to quote, such as AAPL, SPY, ES, or USD/JPY. Batch both "
                        "public companies for a market-cap comparison."
                    ),
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
        description=(
            "Fetch recent candles or compact long-range price extrema for one market symbol. Use mode=extrema "
            "for all-time/record highs, highest closes, lows, or drawdowns from a peak; the server pages through "
            "daily history and returns only processed extrema plus explicit coverage, not raw candles. Pair extrema "
            "with quote when calculating a drawdown from the current live price."
        ),
        parameters=_object_schema(
            {
                "symbol": {"type": "string", "description": "One market symbol such as SPY, AAPL, or NVDA."},
                "mode": {
                    "type": "string",
                    "enum": ["recent", "extrema"],
                    "description": "recent returns bounded candles; extrema returns compact processed long-range highs/lows.",
                },
                "interval": {
                    "type": "string",
                    "description": "Candle interval for recent mode; defaults to 1day. Extrema always processes daily bars.",
                },
                "outputsize": {
                    "type": "integer",
                    "description": "Recent-mode candle count from 1 to 30; defaults to 5. Ignored by extrema mode.",
                },
                "start_date": {"type": "string", "description": "Optional inclusive start date or datetime."},
                "end_date": {"type": "string", "description": "Optional inclusive end date or datetime."},
            },
            required=("symbol",),
        ),
        handler_name="_handle_price_history",
        timeout_seconds=35,
        fallback="If history fails, explain that the symbol or provider lookup failed.",
    ),
    ANNUAL_PERFORMANCE_TOOL_NAME: ToolSpec(
        name=ANNUAL_PERFORMANCE_TOOL_NAME,
        description=(
            "Compute exact calendar-year underlying price changes and cash distributions for up to 5 market "
            "symbols from Yahoo Finance daily history. Use for annual return, dividend, or distribution questions. "
            "A successful result is self-contained for the requested years; do not follow it with quote or "
            "price-history calls unless the user also requested current/intraday data or a required field is missing."
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
    REPORT_RESPONSE_ISSUE_TOOL_NAME: ToolSpec(
        name=REPORT_RESPONSE_ISSUE_TOOL_NAME,
        description=(
            "Archive diagnostics for Nycti's immediately previous response when the user clearly says it was "
            "wrong, stale, misleading, unhelpful, or failed to follow the request. Use this once before correcting "
            "the answer. Do not use it for ordinary follow-up questions, harmless ambiguity, or disagreement where "
            "there is no concrete response-quality problem."
        ),
        parameters=_object_schema(
            {
                "reason": {
                    "type": "string",
                    "description": "A concise description of what was wrong with the previous response.",
                }
            },
            required=("reason",),
        ),
        handler_name="_handle_report_issue",
        timeout_seconds=10,
        fallback="Continue correcting the answer even if diagnostic logging is unavailable.",
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
            "Propose an exact message to a different channel. Never use this for the current channel; put that "
            "content, including mapped member mentions, directly in the reply. Cross-channel sends never execute "
            "directly; the requesting user must confirm the validated target and content with /confirm."
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


def build_registered_tools(
    enabled_names: Collection[str] | None = None,
    *,
    promoted_tool_names: Sequence[str] = (),
) -> list[dict[str, object]]:
    selected = set(enabled_names) if enabled_names is not None else None
    promoted_order = {
        name: index for index, name in enumerate(dict.fromkeys(promoted_tool_names))
    }
    specs = [
        spec for name, spec in TOOL_SPECS.items() if selected is None or name in selected
    ]
    specs.sort(
        key=lambda spec: (
            0 if spec.name in promoted_order else 2 if spec.budget_cost_units > 1 else 1,
            promoted_order.get(spec.name, 0),
        )
    )
    return [spec.openai_schema() for spec in specs]
