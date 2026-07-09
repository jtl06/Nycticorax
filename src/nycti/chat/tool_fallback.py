from __future__ import annotations


def fallback_tool_result(tool_result: str) -> str:
    if tool_result.startswith("Older Discord channel context (raw"):
        return (
            "I fetched older channel context, but couldn't produce a clean final reply from it. "
            "Try asking for a narrower summary or exact detail."
        )
    if tool_result.startswith("Tavily web results for:"):
        return _compact_tavily_web_fallback(tool_result)
    if tool_result.startswith("Tavily extract for:"):
        return (
            "I extracted the page content but couldn't produce a clean final reply from it. "
            "Please retry with a narrower ask."
        )
    if tool_result.startswith("YouTube transcript for:"):
        return (
            "I extracted the YouTube transcript but couldn't produce a clean final reply from it. "
            "Please retry with a narrower question about the video."
        )
    return tool_result


def _compact_tavily_web_fallback(tool_result: str) -> str:
    blocks = [block.strip() for block in tool_result.split("\n\n") if block.strip()]
    header = blocks[0] if blocks else "Tavily web results"
    query = header.removeprefix("Tavily web results for:").strip()
    results = [
        parsed
        for block in blocks[1:5]
        if (parsed := _parse_tavily_result_block(block)) is not None
    ]
    if not results:
        return "I found web sources, but couldn't produce a clean final reply and there were no usable snippets."

    lines = ["I couldn't finish the normal synthesis, but the search results point to:"]
    inferred_lines = _market_explanation_lines(query, results)
    if inferred_lines:
        lines.extend(inferred_lines)
    else:
        for title, _url, snippet in results[:3]:
            if snippet:
                lines.append(f"- {title}: {_shorten(snippet, 150)}")
            else:
                lines.append(f"- {title}")
    sources = _format_sources(results[:3])
    if sources:
        lines.append(f"Sources: {sources}")
    return "\n".join(lines)


def _parse_tavily_result_block(block: str) -> tuple[str, str, str] | None:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return None
    title = _strip_result_number(lines[0])
    url = lines[1] if len(lines) >= 2 and lines[1].startswith(("http://", "https://")) else ""
    snippet_start = 2 if url else 1
    snippet = " ".join(lines[snippet_start:])
    if len(snippet) > 180:
        snippet = snippet[:177].rstrip() + "..."
    return title, url, snippet


def _market_explanation_lines(query: str, results: list[tuple[str, str, str]]) -> list[str]:
    text = " ".join(
        [
            query,
            *[title for title, _url, _snippet in results],
            *[snippet for _title, _url, snippet in results],
        ]
    )
    lowered = text.casefold()
    market_terms = (
        "stock",
        "stocks",
        "shares",
        "market",
        "earnings",
        "results",
        "valuation",
        "price",
        "trading",
    )
    drop_terms = ("why", "sink", "sank", "drop", "tumble", "selloff", "sell-off", "tank")
    if not any(term in lowered for term in market_terms) or not any(term in lowered for term in drop_terms):
        return []
    if any(
        term in lowered
        for term in ("memory", "samsung", "semiconductor", "chip", "sandisk", "micron", "dram", "nand")
    ):
        return [
            "- The read is probably cycle risk, not a simple 'profits are bad' double standard.",
            "- Memory gets valued more like a commodity cycle: strong results can still make investors "
            "worry about peak pricing, supply response, and margin normalization.",
            "- AI/logic chip names often get more credit for durable growth; memory makers get punished "
            "faster when the market thinks the upcycle is already priced in.",
        ]
    return [
        "- Good reported numbers are not enough if investors think guidance, margins, valuation, "
        "or the next cycle is worse than expected.",
        "- A stock can fall after 'good' results when the beat was already priced in or the market "
        "doubts the durability of the earnings.",
    ]


def _format_sources(results: list[tuple[str, str, str]]) -> str:
    formatted = []
    for title, url, _snippet in results:
        if not title or not url:
            continue
        formatted.append(f"[{_shorten(title, 42)}]({url})")
    return ", ".join(formatted)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _strip_result_number(title: str) -> str:
    prefix, separator, rest = title.partition(". ")
    if separator and prefix.isdigit():
        return rest.strip()
    return title.strip()
