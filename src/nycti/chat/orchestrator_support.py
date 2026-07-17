from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.run_state import AgentOutputBudget, AnswerPlan, AnswerProfile
from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from nycti.formatting import extract_think_content
from nycti.llm.responses_adapter import should_use_responses_api

if TYPE_CHECKING:
    from nycti.chat.run_state import AgentRun
    from nycti.config import Settings
    from nycti.llm.client import LLMChatTurn

MIN_CHAT_REPLY_COMPLETION_TOKENS = 700
MIN_TOOL_FOLLOWUP_COMPLETION_TOKENS = 1400
MIN_FINAL_REPLY_COMPLETION_TOKENS = 2000
MIN_HIGH_REASONING_REPLY_TOKENS = 4096
MIN_HIGH_REASONING_TOOL_FOLLOWUP_TOKENS = 6144
MIN_HIGH_REASONING_FINAL_TOKENS = 6144
MIN_HIGH_REASONING_CONTINUATION_TOKENS = 2048
QUICK_REPLY_COMPLETION_TOKENS = 700
QUICK_TOOL_FOLLOWUP_COMPLETION_TOKENS = 1200
QUICK_FINAL_REPLY_COMPLETION_TOKENS = 1400
QUICK_CONTINUATION_TOKENS = 500
LENGTH_CONTINUATION_TOKEN_MARGIN = 0.92
ACTION_TOOL_NAMES = {
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
}
CURRENT_PRICE_REQUEST_RE = re.compile(
    r"\b(?:current\s+price|price\s+of|trading\s+at|last\s+traded|stock\s+(?:doing|price)|"
    r"(?:stock|shares?)\s+(?:now|today)|"
    r"how(?:'s|\s+is)\s+.+\s+(?:stock|ticker)\s+doing)\b",
    re.IGNORECASE,
)
TICKER_CANDIDATE_RE = re.compile(r"\b[A-Z][A-Z0-9.:-]{1,9}\b")
TICKER_STOPWORDS = {
    "AI",
    "API",
    "CEO",
    "CFO",
    "CPU",
    "EPS",
    "ETF",
    "FY",
    "IPO",
    "LLM",
    "NASDAQ",
    "NYSE",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "SEC",
    "USD",
}


def answer_model_for_profile(
    settings: Settings,
    profile: AnswerProfile,
    default_model: str,
) -> str:
    setting_name = {
        AnswerProfile.QUICK: "openai_quick_model",
        AnswerProfile.DEEP: "openai_deep_model",
    }.get(profile, "")
    return str(getattr(settings, setting_name, None) or default_model)


def format_tool_schemas(tools: list[dict[str, object]]) -> str:
    return json.dumps(tools, ensure_ascii=True, indent=2, sort_keys=True)


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


def constrain_answer_plan_to_runtime(
    answer_plan: AnswerPlan,
    tool_runner: object,
    *,
    guild_id: int | None,
    channel_id: int | None,
    source_message_id: int | None,
) -> AnswerPlan:
    availability = getattr(
        getattr(tool_runner, "executor", None),
        "available_tool_names",
        None,
    )
    if not callable(availability):
        return answer_plan
    runtime_names = availability(
        guild_id=guild_id,
        channel_id=channel_id,
        source_message_id=source_message_id,
    )
    unavailable_promoted = tuple(
        name for name in answer_plan.promoted_tool_names if name not in runtime_names
    )
    return replace(
        answer_plan,
        eligible_tool_names=answer_plan.eligible_tool_names.intersection(runtime_names),
        deferred_tool_names=answer_plan.deferred_tool_names.intersection(runtime_names),
        promoted_tool_names=tuple(
            name for name in answer_plan.promoted_tool_names if name in runtime_names
        ),
        unavailable_promoted_tool_names=unavailable_promoted,
    )


def format_available_tool_guidance(
    *,
    available_tool_names: set[str],
    answer_profile: AnswerProfile | None = None,
    promoted_tool_names: tuple[str, ...] = (),
) -> str:
    names = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    lines = [
        "Available tools this turn:\n"
        f"- {names}",
        "Use tools only when useful. Then answer or make a materially different call. "
        "Do not repeat calls or emit textual/XML markup.",
    ]
    promoted = [name for name in promoted_tool_names if name in available_tool_names]
    if promoted:
        lines.append(
            "Likely relevant (nonbinding hint): "
            + ", ".join(promoted)
            + ". Other available tools remain callable. Start with the smallest promoted tool or combination "
            "that fully covers the request."
        )
    if answer_profile == AnswerProfile.DEEP:
        lines.append(
            "Deep mode: prefer one well-scoped deep_research call for multi-source work because it already batches "
            "search, extraction, and reduction. Use direct tools afterward only for a concrete missing requirement; "
            "state conflicts or unresolved uncertainty."
        )
    if WEB_SEARCH_TOOL_NAME in available_tool_names:
        lines.extend(
            [
                "For live/current asks like 'how did X do today', news, releases, schedules, IPO/public status, or "
                "valuation, use web instead of model memory and compare dates.",
                "For unfamiliar public products or versions, use one focused, batched web search.",
                "For requested local or non-English research, query in that language, set country to the English "
                "country name with topic=general, then translate the evidence.",
                "For volatile company-status facts, use current evidence. For earnings, prefer investor-relations "
                "releases, SEC filings, or transcripts; never construct their URLs.",
            ]
        )
    if STOCK_QUOTE_TOOL_NAME in available_tool_names:
        lines.append(
            "For current price asks with a ticker-form symbol, call quote directly, even if unfamiliar. "
            "Treat a bare market symbol or currency pair such as 'what's AAPL?' or 'what's USD/JPY?' as a current "
            "quote unless clearly definitional. Pass FX pairs as BASE/QUOTE. Batch all known requested symbols in "
            "one quote call. Use web first only when identity or listing is unclear; if it surfaces a plausible "
            "ticker, call quote next. Trust quote identity and timestamps over snippets or memory."
        )
        lines.append(
            "For public-company market-cap comparisons or a share price needed to match another company's "
            "valuation, batch both symbols in quote. Use its same-time market-cap and shares-outstanding fields "
            "to calculate the threshold; use web only if those valuation inputs are missing."
        )
        if WEB_SEARCH_TOOL_NAME in available_tool_names:
            lines.append(
                "For a current market, sector, or company-group move, establish breadth and cause: quote a benchmark "
                "and representative or named constituents, and use web for the catalyst. Request both in the same "
                "turn when possible. Do not generalize one company or article to the whole group."
            )
    if available_tool_names & {
        STOCK_QUOTE_TOOL_NAME,
        PRICE_HISTORY_TOOL_NAME,
        ANNUAL_PERFORMANCE_TOOL_NAME,
    }:
        lines.append(
            "Use the market tool matching the requested horizon. Do not add a current quote to a historical or "
            "annual result unless the user requested current data or the specialized result is incomplete."
        )
    if PRICE_HISTORY_TOOL_NAME in available_tool_names:
        lines.append(
            "For ATH, record-high, peak-drawdown, or broader historical-high questions, use price_hist with "
            "mode=extrema. Do not infer an all-time value from recent candles or a dated article. Combine extrema "
            "with quote only when the calculation also needs the current live price."
        )
    if {WEB_SEARCH_TOOL_NAME, STOCK_QUOTE_TOOL_NAME} <= available_tool_names:
        lines.append(
            "For combined public/private valuations, combine market data with a current sourced private valuation; "
            "ignore token pages unless the user asks about a token."
        )
    if EXTRACT_URL_TOOL_NAME in available_tool_names:
        lines.append("For an exact URL, extract it before broad search; do not guess or construct a source URL.")
    if GET_CHANNEL_CONTEXT_TOOL_NAME in available_tool_names:
        lines.append(
            "If the request depends on why another member said something, what changed since an earlier exchange, "
            "or discussion missing from the bounded prompt, use channel_ctx before inferring from stale context."
        )
    if BROWSER_EXTRACT_TOOL_NAME in available_tool_names:
        lines.append("Use browser_extract only after normal url_extract fails on a JavaScript-heavy or blocked page.")
    lines.append("Use the provided local date/time for freshness and relative dates.")
    action_tools = sorted(available_tool_names & ACTION_TOOL_NAMES)
    if action_tools:
        lines.append(
            "Action tools exposed this turn: "
            + ", ".join(action_tools)
            + ". They create validated proposals only and never execute a write; call them only when the user "
            "clearly requested that action, then present the exact server confirmation card."
        )
    return "\n".join(lines)


def quote_verification_prompt_for_price_answer(
    *,
    request_text: str,
    answer_text: str,
    available_tool_names: set[str],
    used_tool_names: set[str],
) -> str | None:
    if STOCK_QUOTE_TOOL_NAME not in available_tool_names or STOCK_QUOTE_TOOL_NAME in used_tool_names:
        return None
    if not CURRENT_PRICE_REQUEST_RE.search(request_text):
        return None
    tickers = extract_ticker_candidates(answer_text) or extract_ticker_candidates(request_text)
    if not tickers:
        return None
    return (
        "You are answering a current price request and identified a possible public ticker "
        f"({', '.join(tickers)}), but have not called quote yet. Call quote next for the likely ticker symbol; "
        "do not search again or answer before that quote result. If quote fails or resolves to the wrong "
        "instrument, explain that briefly."
    )


def extract_ticker_candidates(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for match in TICKER_CANDIDATE_RE.finditer(text):
        ticker = match.group(0).strip(".,:;()[]{}")
        if len(ticker) < 2 or ticker in TICKER_STOPWORDS:
            continue
        if any(char.isdigit() for char in ticker) and not any(char.isalpha() for char in ticker):
            continue
        if ticker not in candidates:
            candidates.append(ticker)
        if len(candidates) >= 3:
            break
    return tuple(candidates)


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


def agent_output_budget(
    settings: Settings,
    answer_profile: AnswerProfile | None = None,
    *,
    hidden_reasoning_effort: str | None = None,
) -> AgentOutputBudget:
    if answer_profile == AnswerProfile.QUICK:
        return AgentOutputBudget(
            reply_tokens=QUICK_REPLY_COMPLETION_TOKENS,
            tool_followup_tokens=QUICK_TOOL_FOLLOWUP_COMPLETION_TOKENS,
            final_tokens=QUICK_FINAL_REPLY_COMPLETION_TOKENS,
            continuation_tokens=QUICK_CONTINUATION_TOKENS,
        )
    reply_tokens = chat_reply_max_tokens(settings)
    if hidden_reasoning_effort == "high":
        reply_tokens = max(reply_tokens, MIN_HIGH_REASONING_REPLY_TOKENS)
        return AgentOutputBudget(
            reply_tokens=reply_tokens,
            tool_followup_tokens=max(
                reply_tokens,
                MIN_HIGH_REASONING_TOOL_FOLLOWUP_TOKENS,
            ),
            final_tokens=max(reply_tokens, MIN_HIGH_REASONING_FINAL_TOKENS),
            continuation_tokens=max(
                MIN_HIGH_REASONING_CONTINUATION_TOKENS,
                min(reply_tokens, 4096),
            ),
        )
    return AgentOutputBudget(
        reply_tokens=reply_tokens,
        tool_followup_tokens=max(reply_tokens, MIN_TOOL_FOLLOWUP_COMPLETION_TOKENS),
        final_tokens=max(reply_tokens, MIN_FINAL_REPLY_COMPLETION_TOKENS),
        continuation_tokens=max(500, min(reply_tokens, 700)),
    )


def record_output_budget_metrics(
    metrics: dict[str, int | str] | None,
    budget: AgentOutputBudget,
) -> None:
    if metrics is None:
        return
    metrics["answer_reply_token_budget"] = budget.reply_tokens
    metrics["answer_tool_followup_token_budget"] = budget.tool_followup_tokens
    metrics["answer_final_token_budget"] = budget.final_tokens
    metrics["answer_continuation_token_budget"] = budget.continuation_tokens


def agent_run_output_budgets(
    settings: Settings,
    *,
    answer_profile: AnswerProfile,
    hidden_reasoning_effort: str | None,
    metrics: dict[str, int | str] | None,
) -> tuple[AgentOutputBudget, AgentOutputBudget]:
    initial = agent_output_budget(
        settings,
        answer_profile,
        hidden_reasoning_effort=hidden_reasoning_effort,
    )
    post_tool = initial
    record_output_budget_metrics(metrics, initial)
    if metrics is not None and post_tool != initial:
        metrics["answer_post_tool_followup_token_budget"] = post_tool.tool_followup_tokens
        metrics["answer_post_tool_final_token_budget"] = post_tool.final_tokens
        metrics["answer_post_tool_continuation_token_budget"] = post_tool.continuation_tokens
    return initial, post_tool


def configured_hidden_reasoning_effort(
    settings: Settings,
    *,
    llm_client: object,
    chat_model: str,
    answer_plan: AnswerPlan,
) -> str | None:
    provider_name = str(
        getattr(getattr(llm_client, "provider_capabilities", None), "name", "")
    )
    if not should_use_responses_api(provider_name=provider_name, model=chat_model):
        return None
    return answer_plan.reasoning_effort_override or str(
        getattr(settings, "openai_reasoning_effort", "") or ""
    )


def output_budget_for_run(
    run: AgentRun,
    *,
    initial: AgentOutputBudget,
    post_tool: AgentOutputBudget,
) -> AgentOutputBudget:
    return post_tool if run.successful_tools else initial


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
