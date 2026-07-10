from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from nycti.chat.context import should_include_channel_aliases_for_prompt
from nycti.chat.run_state import (
    AgentBudget,
    AgentPermissions,
    AnswerPlan,
    AnswerProfile,
)
from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)

if TYPE_CHECKING:
    from nycti.chat.run_state import ToolOutcome

REMINDER_RE = re.compile(r"\b(?:remind\s+me|set\s+(?:a\s+)?reminder|create\s+(?:a\s+)?reminder)\b", re.IGNORECASE)
DEPTH_OVERRIDE_RE = re.compile(
    r"^\s*(?:/depth\s+|depth\s*[:=]\s*)(quick|grounded|deep|auto)\b",
    re.IGNORECASE,
)
DEEP_REQUEST_RE = re.compile(
    r"\b(?:deep[- ]dive|in[- ]depth|rigorous(?:ly)?|thorough(?:ly)?\s+research|"
    r"comprehensive\s+analysis|cross[- ]check|corroborate|multiple\s+(?:independent\s+)?sources|"
    r"conflicting\s+(?:evidence|sources))\b",
    re.IGNORECASE,
)
COMPARATIVE_RESEARCH_RE = re.compile(
    r"\bcompare\b[\s\S]{0,240}\b(?:earnings|guidance|sources?|research|verify|evidence)\b|"
    r"\b(?:earnings|guidance|sources?|research|verify|evidence)\b[\s\S]{0,240}\bcompare\b",
    re.IGNORECASE,
)
GROUNDED_REQUEST_RE = re.compile(
    r"https?://|\b(?:current|currently|latest|today|tonight|recent|news|verify|fact[- ]check|"
    r"sources?|citations?|price|stock|ticker|market|earnings|guidance|ipo|valuation|weather|"
    r"schedule|release|available|availability|calculate|calculation|percentage|transcript|"
    r"youtube|image|older\s+(?:chat|context|discussion)|channel\s+history)\b",
    re.IGNORECASE,
)
QUICK_REQUEST_RE = re.compile(
    r"^\s*(?:hi|hello|hey(?:\s+there)?|thanks|thank\s+you|good\s+(?:morning|night)|"
    r"tell\s+me\s+(?:a|another)\s+joke|write\s+(?:a\s+)?(?:haiku|limerick)|say\s+.{1,80}|"
    r"how\s+did\s+you\s+do\s+that|do\s+you\s+think\s+this\s+.{1,160})"
    r"[.!?]*\s*$",
    re.IGNORECASE,
)
STABLE_EXPLANATION_RE = re.compile(
    r"^\s*(?:what\s+(?:is|are|does)\b|define\b|explain\b|how\s+(?:do|does|can)\b|"
    r"why\s+(?:do|does|is|are)\b)[\s\S]{1,240}[?!.]*\s*$",
    re.IGNORECASE,
)
QUICK_GROUNDING_GUARD_RE = re.compile(
    r"https?://|\b(?:current|currently|latest|today|tonight|recent|news|verify|fact[- ]check|"
    r"sources?|citations?|price|trading|ticker|market\s+cap|valuation\s+of|ipo|schedule|release|"
    r"available|availability|president|prime\s+minister|chief\s+executive|calculate|percentage)\b|"
    r"\$[A-Z][A-Z0-9.:-]{0,9}\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"(?:youtu\.be/|youtube\.com/|\byoutube\b|\bvideo\s+transcript\b)", re.IGNORECASE)
MARKET_RE = re.compile(
    r"\b(?:stocks?|shares?|tickers?|market|earnings|guidance|ipo|listing|valuation|market\s+cap|"
    r"dividends?|dividents?|distributions?|etfs?|indexes?|indices|futures?|nasdaq|nyse|"
    r"public\s+(?:company|yet)|private\s+company)\b|"
    r"\$[A-Z][A-Z0-9.:-]{0,9}\b|\bhow\s+did\s+.{1,80}\s+do\s+(?:today|this\s+week)\b",
    re.IGNORECASE,
)
CURRENT_PRICE_RE = re.compile(
    r"\b(?:current\s+price|latest\s+price|price\s+of|trading\s+at|last\s+traded|quote|"
    r"stock\s+(?:doing|price)|how\s+(?:is|are|did)\s+.{1,80}\s+(?:doing|do\s+today))\b",
    re.IGNORECASE,
)
ANNUAL_MARKET_RE = re.compile(
    r"\b(?:annual|calendar[- ]year|yearly|by\s+year|dividends?|dividents?|distributions?)\b",
    re.IGNORECASE,
)
HISTORICAL_MARKET_RE = re.compile(
    r"\b(?:historical|history|chart|candles?|returns?|performance|since|from\s+\d{4}|"
    r"between\s+\d{4})\b",
    re.IGNORECASE,
)
WEB_RESEARCH_RE = re.compile(
    r"\b(?:current|currently|latest|today|tonight|recent|news|verify|fact[- ]check|sources?|"
    r"citations?|schedule|release|available|availability|research|look\s+up|find\s+out)\b",
    re.IGNORECASE,
)
CALCULATION_RE = re.compile(
    r"\b(?:calculate|calculation|compute|percentage|percent\s+change|sum|compound|cagr)\b|"
    r"\d\s*[-+*/^]\s*\d",
    re.IGNORECASE,
)
CHANNEL_CONTEXT_RE = re.compile(
    r"\b(?:older\s+(?:chat|context|discussion)|channel\s+(?:history|context|earlier)|"
    r"earlier\s+(?:chat|messages)|happened\s+in\s+the\s+channel)\b",
    re.IGNORECASE,
)
IMAGE_REQUEST_RE = re.compile(
    r"\b(?:image|images|photo|photos|picture|pictures|show\s+me\s+what\s+.+\s+looks\s+like)\b",
    re.IGNORECASE,
)
READ_ONLY_TOOL_NAMES = frozenset(
    {
        WEB_SEARCH_TOOL_NAME,
        STOCK_QUOTE_TOOL_NAME,
        PRICE_HISTORY_TOOL_NAME,
        ANNUAL_PERFORMANCE_TOOL_NAME,
        GET_CHANNEL_CONTEXT_TOOL_NAME,
        IMAGE_SEARCH_TOOL_NAME,
        EXTRACT_URL_TOOL_NAME,
        BROWSER_EXTRACT_TOOL_NAME,
        YOUTUBE_TRANSCRIPT_TOOL_NAME,
        PYTHON_EXEC_TOOL_NAME,
    }
)

QUICK_AGENT_BUDGET = AgentBudget(
    max_model_turns=2,
    max_tool_calls=2,
    max_corrections=1,
    max_continuations=0,
    total_timeout_seconds=18.0,
    finalization_reserve_seconds=4.0,
)
DEEP_AGENT_BUDGET = AgentBudget(
    max_model_turns=8,
    max_tool_calls=16,
    max_corrections=1,
    max_continuations=1,
    total_timeout_seconds=60.0,
    finalization_reserve_seconds=10.0,
)


def select_answer_plan(
    *,
    request_text: str,
    guild_id: int | None,
    default_budget: AgentBudget | None = None,
    depth_override: AnswerProfile | str | None = None,
) -> tuple[AnswerPlan, AgentPermissions]:
    base_budget = default_budget or AgentBudget()
    profile, reason, explicit = _select_profile(
        request_text=request_text,
        depth_override=depth_override,
    )
    reminder_allowed = guild_id is not None and bool(REMINDER_RE.search(request_text))
    send_allowed = guild_id is not None and should_include_channel_aliases_for_prompt(
        prompt=request_text,
        context_text="",
    )

    selected = _select_read_tools(profile, request_text)
    if reminder_allowed:
        selected.add(CREATE_REMINDER_TOOL_NAME)
    if send_allowed:
        selected.add(SEND_CHANNEL_MESSAGE_TOOL_NAME)

    plan = AnswerPlan(
        profile=profile,
        eligible_tool_names=frozenset(selected),
        budget=_profile_budget(profile, base_budget),
        reasoning_effort_override={
            AnswerProfile.QUICK: "low",
            AnswerProfile.GROUNDED: None,
            AnswerProfile.DEEP: "high",
        }[profile],
        selection_reason=reason,
        explicit_override=explicit,
    )
    permissions = AgentPermissions(
        allow_reminders=reminder_allowed,
        allow_cross_channel_send=send_allowed,
    )
    return plan, permissions


def _select_profile(
    *,
    request_text: str,
    depth_override: AnswerProfile | str | None,
) -> tuple[AnswerProfile, str, bool]:
    override, explicit = _resolve_depth_override(request_text, depth_override)
    if override is not None:
        return override, f"explicit_{override}", True

    detection_text = (
        DEPTH_OVERRIDE_RE.sub("", request_text, count=1).strip()
        if explicit
        else request_text
    )
    if DEEP_REQUEST_RE.search(detection_text) or COMPARATIVE_RESEARCH_RE.search(detection_text):
        profile = AnswerProfile.DEEP
        reason = "deep_research_signal"
    elif _is_quick_request(detection_text):
        profile = AnswerProfile.QUICK
        reason = "simple_conversation_signal"
    elif GROUNDED_REQUEST_RE.search(detection_text) or REMINDER_RE.search(detection_text):
        profile = AnswerProfile.GROUNDED
        reason = "grounding_signal"
    else:
        profile = AnswerProfile.GROUNDED
        reason = "ambiguous_default"
    if explicit:
        reason = f"explicit_auto:{reason}"
    return profile, reason, explicit


def _resolve_depth_override(
    request_text: str,
    depth_override: AnswerProfile | str | None,
) -> tuple[AnswerProfile | None, bool]:
    match = DEPTH_OVERRIDE_RE.match(request_text)
    if match is not None:
        raw_override = match.group(1)
        explicit = True
    else:
        raw_override = depth_override
        explicit = depth_override is not None
    if raw_override is None:
        return None, False
    if isinstance(raw_override, AnswerProfile):
        return raw_override, explicit
    normalized = str(raw_override).strip().casefold()
    if normalized == "auto":
        return None, explicit
    try:
        return AnswerProfile(normalized), explicit
    except ValueError:
        return None, False


def _profile_budget(profile: AnswerProfile, base: AgentBudget) -> AgentBudget:
    if profile == AnswerProfile.GROUNDED:
        return base
    if profile == AnswerProfile.QUICK:
        timeout = min(base.total_timeout_seconds, QUICK_AGENT_BUDGET.total_timeout_seconds)
        return AgentBudget(
            max_model_turns=min(base.max_model_turns, QUICK_AGENT_BUDGET.max_model_turns),
            max_tool_calls=min(base.max_tool_calls, QUICK_AGENT_BUDGET.max_tool_calls),
            max_corrections=min(base.max_corrections, QUICK_AGENT_BUDGET.max_corrections),
            max_continuations=0,
            total_timeout_seconds=timeout,
            finalization_reserve_seconds=min(
                base.finalization_reserve_seconds,
                QUICK_AGENT_BUDGET.finalization_reserve_seconds,
                timeout * 0.4,
            ),
        )
    return AgentBudget(
        max_model_turns=max(base.max_model_turns, DEEP_AGENT_BUDGET.max_model_turns),
        max_tool_calls=max(base.max_tool_calls, DEEP_AGENT_BUDGET.max_tool_calls),
        max_corrections=max(base.max_corrections, DEEP_AGENT_BUDGET.max_corrections),
        max_continuations=max(base.max_continuations, DEEP_AGENT_BUDGET.max_continuations),
        total_timeout_seconds=max(
            base.total_timeout_seconds,
            DEEP_AGENT_BUDGET.total_timeout_seconds,
        ),
        finalization_reserve_seconds=max(
            base.finalization_reserve_seconds,
            DEEP_AGENT_BUDGET.finalization_reserve_seconds,
        ),
    )


def _is_quick_request(request_text: str) -> bool:
    if QUICK_REQUEST_RE.fullmatch(request_text):
        return True
    return bool(
        STABLE_EXPLANATION_RE.fullmatch(request_text)
        and not QUICK_GROUNDING_GUARD_RE.search(request_text)
    )


def _select_read_tools(profile: AnswerProfile, request_text: str) -> set[str]:
    if profile == AnswerProfile.QUICK:
        return set()
    if profile == AnswerProfile.DEEP:
        return set(READ_ONLY_TOOL_NAMES)

    selected: set[str] = set()
    matched = False
    if URL_RE.search(request_text):
        selected.update({EXTRACT_URL_TOOL_NAME, WEB_SEARCH_TOOL_NAME})
        matched = True
    if YOUTUBE_RE.search(request_text):
        selected.update({YOUTUBE_TRANSCRIPT_TOOL_NAME, EXTRACT_URL_TOOL_NAME})
        matched = True
    if CHANNEL_CONTEXT_RE.search(request_text):
        selected.add(GET_CHANNEL_CONTEXT_TOOL_NAME)
        matched = True
    if IMAGE_REQUEST_RE.search(request_text):
        selected.add(IMAGE_SEARCH_TOOL_NAME)
        matched = True
    if CALCULATION_RE.search(request_text):
        selected.add(PYTHON_EXEC_TOOL_NAME)
        matched = True
    if CURRENT_PRICE_RE.search(request_text):
        selected.update({STOCK_QUOTE_TOOL_NAME, WEB_SEARCH_TOOL_NAME})
        matched = True
    if MARKET_RE.search(request_text):
        selected.add(WEB_SEARCH_TOOL_NAME)
        if ANNUAL_MARKET_RE.search(request_text):
            selected.update({ANNUAL_PERFORMANCE_TOOL_NAME, EXTRACT_URL_TOOL_NAME})
        elif HISTORICAL_MARKET_RE.search(request_text):
            selected.update({PRICE_HISTORY_TOOL_NAME, STOCK_QUOTE_TOOL_NAME})
        else:
            selected.add(STOCK_QUOTE_TOOL_NAME)
            if not CURRENT_PRICE_RE.search(request_text):
                selected.add(EXTRACT_URL_TOOL_NAME)
        matched = True
    if WEB_RESEARCH_RE.search(request_text) and not CHANNEL_CONTEXT_RE.search(request_text):
        selected.add(WEB_SEARCH_TOOL_NAME)
        if not CURRENT_PRICE_RE.search(request_text):
            selected.add(EXTRACT_URL_TOOL_NAME)
        matched = True
    return selected if matched else set(READ_ONLY_TOOL_NAMES)


def select_eligible_tools(
    *,
    request_text: str,
    guild_id: int | None,
    depth_override: AnswerProfile | str | None = None,
) -> tuple[set[str], AgentPermissions]:
    plan, permissions = select_answer_plan(
        request_text=request_text,
        guild_id=guild_id,
        depth_override=depth_override,
    )
    return set(plan.eligible_tool_names), permissions


def expand_tools_from_outcomes(
    selected: set[str],
    outcomes: Iterable[ToolOutcome],
) -> set[str]:
    expanded = set(selected)
    if any(
        outcome.tool_name == EXTRACT_URL_TOOL_NAME and outcome.status != "ok"
        for outcome in outcomes
    ):
        expanded.add(BROWSER_EXTRACT_TOOL_NAME)
    return expanded
