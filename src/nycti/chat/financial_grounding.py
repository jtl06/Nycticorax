from __future__ import annotations

from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)

SOURCE_EXTRACTION_TOOLS = frozenset(
    {
        EXTRACT_URL_TOOL_NAME,
        BROWSER_EXTRACT_TOOL_NAME,
    }
)


def append_financial_history_guidance(guidance: str, *, enabled: bool) -> str:
    if not enabled:
        return guidance
    return guidance + (
        "\nFor this annual dividend/performance request, use annual_perf as the primary evidence. Report its "
        "calendar-year cash-distribution percentage and underlying price change without replacing either with "
        "total return. If web search is also used, treat snippets as leads and extract the strongest source page. "
        "Do not estimate price change by subtracting distribution yield from total return. If an exact value "
        "cannot be verified, mark it unavailable instead of guessing. Include source URLs."
    )


def needs_financial_source_extraction(
    *,
    enabled: bool,
    used_tools: set[str],
    available_tools: set[str],
) -> bool:
    return bool(
        enabled
        and WEB_SEARCH_TOOL_NAME in used_tools
        and not SOURCE_EXTRACTION_TOOLS & used_tools
        and SOURCE_EXTRACTION_TOOLS & available_tools
    )


def append_financial_source_instruction(
    messages: list[dict[str, object]],
    *,
    search_just_finished: bool,
) -> None:
    if search_just_finished:
        content = (
            "Use the search results as leads. Extract the strongest relevant source URL next instead of "
            "repeating a similar broad search."
        )
    else:
        content = (
            "Before answering, call url_extract or browser_extract on the strongest relevant source URL from the "
            "search results. Do not answer from snippets or estimates alone."
        )
    messages.append({"role": "user", "content": content})
