from __future__ import annotations

from dataclasses import dataclass

from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    name: str
    skill: str
    when_to_use: str
    cost: str
    risk: str
    required_env: tuple[str, ...] = ()
    permission: str = "triggered_request"
    fallback: str = "Explain the failed tool result briefly and answer from available context."


TOOL_METADATA: dict[str, ToolMetadata] = {
    WEB_SEARCH_TOOL_NAME: ToolMetadata(
        name=WEB_SEARCH_TOOL_NAME,
        skill="fresh_web",
        when_to_use="Use for current public facts, news, docs, product info, or facts likely to have changed.",
        cost="external_api",
        risk="medium",
        required_env=("TAVILY_API_KEY",),
        fallback="If search fails, say fresh web lookup failed and avoid guessing current facts.",
    ),
    STOCK_QUOTE_TOOL_NAME: ToolMetadata(
        name=STOCK_QUOTE_TOOL_NAME,
        skill="market_quote",
        when_to_use=(
            "Use for latest market quotes for up to 5 supported symbols; outside regular hours, "
            "it appends Yahoo Finance pre/post-market fallback data when available."
        ),
        cost="external_api",
        risk="high",
        required_env=("TWELVE_DATA_API_KEY",),
        fallback="If quote fails, explain the provider/symbol issue and avoid inventing prices.",
    ),
    PRICE_HISTORY_TOOL_NAME: ToolMetadata(
        name=PRICE_HISTORY_TOOL_NAME,
        skill="market_history",
        when_to_use="Use for recent candles, prior closes, short trend windows, and dated market lookbacks.",
        cost="external_api",
        risk="high",
        required_env=("TWELVE_DATA_API_KEY",),
        fallback="If history fails, explain that the symbol or provider lookup failed.",
    ),
    GET_CHANNEL_CONTEXT_TOOL_NAME: ToolMetadata(
        name=GET_CHANNEL_CONTEXT_TOOL_NAME,
        skill="discord_context",
        when_to_use="Use when the default recent Discord window is insufficient for a reply or summary.",
        cost="llm_optional",
        risk="low",
        fallback="If context fetch fails, answer from the recent context and mention the gap only if material.",
    ),
    IMAGE_SEARCH_TOOL_NAME: ToolMetadata(
        name=IMAGE_SEARCH_TOOL_NAME,
        skill="image_lookup",
        when_to_use="Use when the user asks what something looks like or wants an example image.",
        cost="external_api",
        risk="medium",
        required_env=("TAVILY_API_KEY",),
        fallback="If image search fails, answer text-only.",
    ),
    EXTRACT_URL_TOOL_NAME: ToolMetadata(
        name=EXTRACT_URL_TOOL_NAME,
        skill="url_extract",
        when_to_use="Use for a specific public URL before using generic web search.",
        cost="external_api",
        risk="medium",
        required_env=("TAVILY_API_KEY",),
        fallback="If extraction is thin or blocked, try browser_extract_content when configured.",
    ),
    BROWSER_EXTRACT_TOOL_NAME: ToolMetadata(
        name=BROWSER_EXTRACT_TOOL_NAME,
        skill="browser_extract",
        when_to_use="Use for JS-heavy or blocked pages after normal URL extraction fails or returns thin content.",
        cost="local_browser",
        risk="medium",
        required_env=("BROWSER_TOOL_ENABLED",),
        fallback="If browser extraction fails, summarize from available URL/search context.",
    ),
    YOUTUBE_TRANSCRIPT_TOOL_NAME: ToolMetadata(
        name=YOUTUBE_TRANSCRIPT_TOOL_NAME,
        skill="youtube_transcript",
        when_to_use="Use for YouTube video summaries, transcript extraction, quotes, or questions about spoken video content.",
        cost="external_http+llm",
        risk="medium",
        required_env=("YOUTUBE_TRANSCRIPT_ENABLED",),
        fallback="If transcript extraction fails, say the transcript was unavailable and avoid pretending to know the video contents.",
    ),
    UPDATE_PERSONAL_PROFILE_TOOL_NAME: ToolMetadata(
        name=UPDATE_PERSONAL_PROFILE_TOOL_NAME,
        skill="profile_memory",
        when_to_use="Use sparingly when durable personal context changed and should update the compact profile note.",
        cost="llm",
        risk="medium",
        fallback="Skip the update; do not block the user-facing reply.",
    ),
    PYTHON_EXEC_TOOL_NAME: ToolMetadata(
        name=PYTHON_EXEC_TOOL_NAME,
        skill="python_calculation",
        when_to_use="Use for math, small data transforms, sanity checks, or preparing precise table values.",
        cost="local_cpu",
        risk="high",
        required_env=("PYTHON_TOOL_ENABLED",),
        permission="all_users_when_enabled",
        fallback="If Python is disabled or rejected by the sandbox, answer without executing code.",
    ),
    CREATE_REMINDER_TOOL_NAME: ToolMetadata(
        name=CREATE_REMINDER_TOOL_NAME,
        skill="reminder_write",
        when_to_use="Use when the user asks Nycti to remind them at a future time.",
        cost="database",
        risk="medium",
        fallback="Ask for a clearer future time if reminder creation fails from ambiguity.",
    ),
    SEND_CHANNEL_MESSAGE_TOOL_NAME: ToolMetadata(
        name=SEND_CHANNEL_MESSAGE_TOOL_NAME,
        skill="discord_send",
        when_to_use="Use only when the user explicitly asks to post a message to another channel.",
        cost="discord_api",
        risk="high",
        permission="explicit_user_request",
        fallback="Do not send anything if target channel or permission is unclear.",
    ),
}


def get_tool_metadata(name: str) -> ToolMetadata | None:
    return TOOL_METADATA.get(name)


def build_tool_planner_catalog(tool_names: set[str]) -> str:
    rows: list[str] = []
    for name in sorted(tool_names):
        metadata = TOOL_METADATA.get(name)
        if metadata is None:
            rows.append(f"- {name}: no metadata")
            continue
        env_text = ",".join(metadata.required_env) if metadata.required_env else "none"
        rows.append(
            f"- {name}: skill={metadata.skill}; use={metadata.when_to_use}; "
            f"cost={metadata.cost}; risk={metadata.risk}; env={env_text}; permission={metadata.permission}"
        )
    return "\n".join(rows)
