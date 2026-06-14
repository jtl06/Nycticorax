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

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
YOUTUBE_URL_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE)
REMINDER_RE = re.compile(r"\b(?:remind\s+me|set\s+(?:a\s+)?reminder|create\s+(?:a\s+)?reminder)\b", re.IGNORECASE)
IMAGE_RE = re.compile(
    r"\b(?:image|photo|picture|pic|what\s+does.+look\s+like|show\s+me|see\s+an?\s+example)\b",
    re.IGNORECASE | re.DOTALL,
)
OLDER_CONTEXT_RE = re.compile(
    r"\b(?:older|earlier|previously|channel\s+history|conversation\s+history|what\s+did.+say|"
    r"summarize.+(?:channel|conversation|chat))\b",
    re.IGNORECASE | re.DOTALL,
)
QUOTE_RE = re.compile(
    r"\b(?:stock|ticker|quote|trading|market|pre[- ]?market|after[- ]?hours|overnight|"
    r"futures?|price)\b",
    re.IGNORECASE,
)
EXPLICIT_TICKER_RE = re.compile(
    r"(?i:(?<!\w)\$[A-Z][A-Z0-9.-]{0,9}\b"
    r"|\b[A-Z][A-Z0-9.-]{0,9}\s+ticker\b"
    r"|\bticker(?:\s+symbol)?\s+(?:is\s+)?[A-Z][A-Z0-9.-]{0,9}\b)"
    r"|\b[A-Z]{1,5}(?:\.[A-Z])?\b",
)
PRICE_HISTORY_RE = re.compile(
    r"\b(?:price\s+history|historical|chart|candles?|all[- ]time[- ]high|ath|"
    r"performance|return|prior\s+close|past\s+\d+|since\s+\d{4})\b",
    re.IGNORECASE,
)
FINANCIAL_HISTORY_RE = re.compile(
    r"\b(?:dividends?|dividents?|distributions?|(?:distribution|dividend)\s+yield|"
    r"annual\s+(?:price\s+)?(?:change|performance|returns?)|"
    r"yearly\s+(?:price\s+)?(?:change|performance|returns?)|"
    r"(?:price|underlying)\s+change.{0,30}by\s+year|"
    r"by\s+year.{0,30}(?:price|underlying|performance|returns?))\b",
    re.IGNORECASE,
)
PYTHON_RE = re.compile(
    r"\b(?:calculate|compute|math|probability|formula|percentage|percent|statistics?|"
    r"parse|transform|python|code)\b",
    re.IGNORECASE,
)


def select_eligible_tools(
    *,
    request_text: str,
    search_requested: bool,
    guild_id: int | None,
) -> tuple[set[str], AgentPermissions]:
    selected = {
        WEB_SEARCH_TOOL_NAME,
        STOCK_QUOTE_TOOL_NAME,
        ANNUAL_PERFORMANCE_TOOL_NAME,
    }
    has_url = bool(URL_RE.search(request_text))
    if PRICE_HISTORY_RE.search(request_text):
        selected.add(PRICE_HISTORY_TOOL_NAME)
    if PYTHON_RE.search(request_text):
        selected.add(PYTHON_EXEC_TOOL_NAME)
    if has_url:
        selected.update({EXTRACT_URL_TOOL_NAME, BROWSER_EXTRACT_TOOL_NAME})
    if YOUTUBE_URL_RE.search(request_text):
        selected.add(YOUTUBE_TRANSCRIPT_TOOL_NAME)
    if IMAGE_RE.search(request_text):
        selected.add(IMAGE_SEARCH_TOOL_NAME)
    if guild_id is not None and OLDER_CONTEXT_RE.search(request_text):
        selected.add(GET_CHANNEL_CONTEXT_TOOL_NAME)

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
    required: set[str] = set()
    if search_requested:
        required.add(WEB_SEARCH_TOOL_NAME)
    if is_financial_history_request(request_text):
        required.add(ANNUAL_PERFORMANCE_TOOL_NAME)
    if required:
        return required
    if not QUOTE_RE.search(request_text):
        return set()
    return (
        {STOCK_QUOTE_TOOL_NAME}
        if has_explicit_ticker(request_text)
        else {WEB_SEARCH_TOOL_NAME}
    )


def has_explicit_ticker(request_text: str) -> bool:
    return bool(EXPLICIT_TICKER_RE.search(request_text))


def is_financial_history_request(request_text: str) -> bool:
    return bool(FINANCIAL_HISTORY_RE.search(request_text))


def expand_tools_from_outcomes(
    selected: set[str],
    outcomes: Iterable[ToolOutcome],
) -> set[str]:
    expanded = set(selected)
    combined = "\n".join(outcome.content for outcome in outcomes)
    if URL_RE.search(combined):
        expanded.update({EXTRACT_URL_TOOL_NAME, BROWSER_EXTRACT_TOOL_NAME})
    return expanded
