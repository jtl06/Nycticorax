from __future__ import annotations

from nycti.tavily.models import TavilySearchResponse


def format_tavily_search_message(response: TavilySearchResponse, *, max_items: int = 5) -> str:
    if not response.results:
        return f"No web results found for: {response.query}"

    lines = [f"Tavily web results for: {response.query}"]
    for index, result in enumerate(response.results[:max_items], start=1):
        snippet = " ".join(result.content.split())
        if len(snippet) > 180:
            snippet = f"{snippet[:177]}..."
        line = f"{index}. {result.title}\n{result.url}"
        if snippet:
            line += f"\n{snippet}"
        lines.append(line)
    return "\n\n".join(lines)
