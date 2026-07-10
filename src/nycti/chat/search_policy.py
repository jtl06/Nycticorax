from __future__ import annotations

import re


CURRENT_MARKET_SEARCH_TERMS = (
    " stock",
    "ticker",
    "share price",
    "trading",
    "nasdaq",
    "nyse",
    "ipo",
    "valuation",
    "valued at",
    "market cap",
    "market capitalization",
    "market price",
)
HISTORICAL_SEARCH_TERMS = (
    "historical",
    "history",
    "previously",
    "formerly",
    "in the past",
    "at the time",
    "as of",
)
EXPLICIT_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
RELATIVE_HISTORICAL_PATTERN = re.compile(
    r"\b(?:last|past|prior|previous)\s+(?:\d+\s+)?(?:day|week|month|quarter|year)s?\b"
)
PRIMARY_SOURCE_TERMS = (
    "earnings",
    "guidance",
    "investor relations",
    "press release",
    "sec filing",
)


def web_search_options_for_query(
    query: str,
    *,
    configured_depth: str,
) -> dict[str, str | None]:
    normalized = query.casefold()
    if any(term in f" {normalized}" for term in CURRENT_MARKET_SEARCH_TERMS):
        return {
            "search_depth": "basic",
            "topic": "finance",
            "time_range": (
                None
                if EXPLICIT_YEAR_PATTERN.search(normalized)
                or RELATIVE_HISTORICAL_PATTERN.search(normalized)
                or any(term in normalized for term in HISTORICAL_SEARCH_TERMS)
                else "week"
            ),
        }
    if configured_depth != "ultra-fast":
        return {"search_depth": None}
    return {
        "search_depth": (
            "basic" if any(term in normalized for term in PRIMARY_SOURCE_TERMS) else None
        )
    }
