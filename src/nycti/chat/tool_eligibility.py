from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from nycti.chat.context import should_include_channel_aliases_for_prompt
from nycti.chat.run_state import AgentPermissions
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
VOLATILE_COMPANY_STATUS_RE = re.compile(
    r"\b(?:"
    r"ipo(?:'d|ed)?|public|private|listed|listing|delisted|ticker|valuation|"
    r"market\s*cap|market\s*capitalization"
    r")\b",
    re.IGNORECASE,
)
DEFINITION_QUESTION_RE = re.compile(
    r"\b(?:what\s+(?:does|is|are)|define|explain)\b.{0,60}\b(?:mean|definition|concept)\b",
    re.IGNORECASE | re.DOTALL,
)
HOW_DID_YOU_RE = re.compile(r"\bhow\s+did\s+you\s+do\b", re.IGNORECASE)
CURRENT_PERFORMANCE_RE = re.compile(
    r"\b(?:"
    r"how\s+(?:did|is|are|was|were)\b.{0,80}\b(?:do|doing|perform|performing)|"
    r"what\s+happened\s+to\b.{0,80}\b(?:today|now|recently|this\s+week)|"
    r"(?:stock|shares?)\b.{0,40}\b(?:doing|today|now|currently|trading)"
    r")",
    re.IGNORECASE | re.DOTALL,
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


def select_eligible_tools(
    *,
    request_text: str,
    search_requested: bool,
    guild_id: int | None,
) -> tuple[set[str], AgentPermissions]:
    selected = set(READ_ONLY_TOOL_NAMES)
    reminder_allowed = guild_id is not None and bool(REMINDER_RE.search(request_text))
    send_allowed = guild_id is not None and should_include_channel_aliases_for_prompt(
        prompt=request_text,
        context_text="",
    )
    if reminder_allowed:
        selected.add(CREATE_REMINDER_TOOL_NAME)
    if send_allowed:
        selected.add(SEND_CHANNEL_MESSAGE_TOOL_NAME)

    permissions = AgentPermissions(
        allow_reminders=reminder_allowed,
        allow_cross_channel_send=send_allowed,
    )
    return selected, permissions


def required_tools_for_request(
    *,
    request_text: str,
    search_requested: bool,
) -> set[str]:
    if search_requested or requires_fresh_web_grounding(request_text):
        return {WEB_SEARCH_TOOL_NAME}
    return set()


def requires_fresh_web_grounding(request_text: str) -> bool:
    if DEFINITION_QUESTION_RE.search(request_text) or HOW_DID_YOU_RE.search(request_text):
        return False
    return bool(
        VOLATILE_COMPANY_STATUS_RE.search(request_text)
        or CURRENT_PERFORMANCE_RE.search(request_text)
    )


def expand_tools_from_outcomes(
    selected: set[str],
    outcomes: Iterable[ToolOutcome],
) -> set[str]:
    return set(selected)
