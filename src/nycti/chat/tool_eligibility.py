from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from nycti.chat.run_state import ToolOutcome

DEPTH_OVERRIDE_RE = re.compile(
    r"^\s*(?:/depth\s+|depth\s*[:=]\s*|(?=(?:quick|grounded|deep)\s*:))"
    r"(quick|grounded|deep|auto)\b\s*:?\s*",
    re.IGNORECASE,
)
DEEP_REQUEST_RE = re.compile(
    r"\b(?:deep[- ]dive|in[- ]depth|rigorous(?:ly)?|thorough(?:ly)?\s+research|"
    r"comprehensive\s+analysis|cross[- ]check|corroborate|multiple\s+(?:independent\s+)?sources|"
    r"conflicting\s+(?:evidence|sources))\b",
    re.IGNORECASE,
)
QUICK_REQUEST_RE = re.compile(
    r"^\s*(?:hi|hello|hey(?:\s+there)?|thanks|thank\s+you|good\s+(?:morning|night)|"
    r"tell\s+me\s+(?:a|another)\s+joke|write\s+(?:a\s+)?(?:haiku|limerick)|say\s+.{1,80}|"
    r"how\s+did\s+you\s+do\s+that|do\s+you\s+think\s+this\s+.{1,160})"
    r"[.!?]*\s*$",
    re.IGNORECASE,
)
QUICK_EXPLANATION_RE = re.compile(
    r"^\s*(?:what\s+(?:is|are|does)\b|define\b|explain\b|how\s+(?:do|does|can)\b|"
    r"why\s+(?:do|does|is|are)\b)[\s\S]{1,240}[?!.]*\s*$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://", re.IGNORECASE)
YOUTUBE_URL_RE = re.compile(r"https?://(?:www\.)?(?:youtu\.be/|youtube\.com/)", re.IGNORECASE)
CALCULATION_RE = re.compile(r"\d\s*[-+*/^]\s*\d")
CHANNEL_CONTEXT_RE = re.compile(
    r"\b(?:older\s+(?:chat|context|discussion)|channel\s+(?:history|context|earlier)|"
    r"earlier\s+(?:chat|messages)|happened\s+in\s+the\s+channel)\b",
    re.IGNORECASE,
)
IMAGE_REQUEST_RE = re.compile(
    r"\b(?:image|images|photo|photos|picture|pictures)\b",
    re.IGNORECASE,
)
DOLLAR_TICKER_RE = re.compile(r"\$[A-Z][A-Z0-9.-]{0,9}\b")
READ_ONLY_TOOL_NAMES = frozenset(
    {
        DEEP_RESEARCH_TOOL_NAME,
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
        MEMORY_SEARCH_TOOL_NAME,
    }
)
# The catalog is small enough to preload every safe read schema.  When it
# becomes materially larger, expensive/niche tools can move to the deferred
# tier once a search/describe/call resolver exists.  Until then, an empty
# deferred tier is deliberate: no read capability is hidden by prompt text.
DIRECT_READ_TOOL_NAMES = READ_ONLY_TOOL_NAMES
DEFERRED_READ_TOOL_NAMES: frozenset[str] = frozenset()
QUICK_AGENT_BUDGET = AgentBudget(
    # Leave one recovery turn for an empty or mistaken read-tool choice. This
    # does not slow ordinary one-turn replies, and keeps a corrected grounded
    # answer inside the normal loop instead of forcing finalization.
    max_model_turns=3,
    max_tool_calls=12,
    max_deep_research_calls=1,
    max_corrections=4,
    max_continuations=0,
    total_timeout_seconds=18.0,
    finalization_reserve_seconds=4.0,
)
DEEP_AGENT_BUDGET = AgentBudget(
    max_model_turns=8,
    max_tool_calls=16,
    max_deep_research_calls=1,
    max_corrections=4,
    max_continuations=1,
    total_timeout_seconds=60.0,
    finalization_reserve_seconds=10.0,
)


def select_answer_plan(
    *,
    request_text: str,
    context_text: str = "",
    guild_id: int | None,
    default_budget: AgentBudget | None = None,
    depth_override: AnswerProfile | str | None = None,
) -> tuple[AnswerPlan, AgentPermissions]:
    base_budget = default_budget or AgentBudget()
    profile, reason, explicit = _select_profile(
        request_text=request_text,
        depth_override=depth_override,
    )
    tool_request_text = request_text
    depth_match = DEPTH_OVERRIDE_RE.match(request_text)
    if depth_match is not None:
        tool_request_text = DEPTH_OVERRIDE_RE.sub("", request_text, count=1).strip()
    promoted_tools = list(_promote_read_tools(tool_request_text))
    if profile == AnswerProfile.DEEP and DEEP_RESEARCH_TOOL_NAME not in promoted_tools:
        promoted_tools.insert(0, DEEP_RESEARCH_TOOL_NAME)
    promoted = tuple(promoted_tools)
    selected = set(DIRECT_READ_TOOL_NAMES)
    # Guild-only tools include server-validated action proposals and local
    # response-quality feedback. Prompt meaning never grants write authority;
    # explicit confirmation mints action capabilities.
    if guild_id is not None:
        selected.update(
            {
                CREATE_REMINDER_TOOL_NAME,
                REPORT_RESPONSE_ISSUE_TOOL_NAME,
                SEND_CHANNEL_MESSAGE_TOOL_NAME,
            }
        )

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
        promoted_tool_names=promoted,
        deferred_tool_names=DEFERRED_READ_TOOL_NAMES,
    )
    return plan, AgentPermissions()


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
    if DEEP_REQUEST_RE.search(detection_text):
        profile = AnswerProfile.DEEP
        reason = "deep_research_signal"
    elif _is_quick_request(detection_text):
        profile = AnswerProfile.QUICK
        reason = "simple_conversation_signal"
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
            # A latency profile may shorten model work, but it must not revoke
            # grounding capacity or recovery paths.
            max_tool_calls=base.max_tool_calls,
            max_deep_research_calls=min(
                base.max_deep_research_calls,
                QUICK_AGENT_BUDGET.max_deep_research_calls,
            ),
            max_corrections=base.max_corrections,
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
        max_deep_research_calls=max(
            base.max_deep_research_calls,
            DEEP_AGENT_BUDGET.max_deep_research_calls,
        ),
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
    return bool(
        QUICK_REQUEST_RE.fullmatch(request_text)
        or QUICK_EXPLANATION_RE.fullmatch(request_text)
    )


def _promote_read_tools(request_text: str) -> tuple[str, ...]:
    """Return optional relevance hints without changing tool reachability."""
    promoted: list[str] = []

    def promote(*names: str) -> None:
        for name in names:
            if name not in promoted:
                promoted.append(name)

    if DEEP_REQUEST_RE.search(request_text):
        promote(DEEP_RESEARCH_TOOL_NAME)
    if YOUTUBE_URL_RE.search(request_text):
        promote(YOUTUBE_TRANSCRIPT_TOOL_NAME, EXTRACT_URL_TOOL_NAME)
    elif URL_RE.search(request_text):
        promote(EXTRACT_URL_TOOL_NAME)
    if CHANNEL_CONTEXT_RE.search(request_text):
        promote(GET_CHANNEL_CONTEXT_TOOL_NAME)
    if IMAGE_REQUEST_RE.search(request_text):
        promote(IMAGE_SEARCH_TOOL_NAME)
    if CALCULATION_RE.search(request_text):
        promote(PYTHON_EXEC_TOOL_NAME)
    if DOLLAR_TICKER_RE.search(request_text):
        promote(STOCK_QUOTE_TOOL_NAME)
    return tuple(promoted)


def select_eligible_tools(
    *,
    request_text: str,
    context_text: str = "",
    guild_id: int | None,
    depth_override: AnswerProfile | str | None = None,
) -> tuple[set[str], AgentPermissions]:
    plan, permissions = select_answer_plan(
        request_text=request_text,
        context_text=context_text,
        guild_id=guild_id,
        depth_override=depth_override,
    )
    return set(plan.eligible_tool_names), permissions


def expand_tools_from_outcomes(
    selected: set[str],
    outcomes: Iterable[ToolOutcome],
    *,
    reachable_tool_names: Iterable[str] | None = None,
) -> set[str]:
    expanded = set(selected)
    if any(
        outcome.tool_name == EXTRACT_URL_TOOL_NAME and outcome.status != "ok"
        for outcome in outcomes
    ):
        expanded.add(BROWSER_EXTRACT_TOOL_NAME)
    if reachable_tool_names is not None:
        expanded.intersection_update(reachable_tool_names)
    return expanded
