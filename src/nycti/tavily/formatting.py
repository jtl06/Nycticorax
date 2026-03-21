from __future__ import annotations

from nycti.tavily.models import TavilyExtractResponse, TavilySearchResponse


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


def format_tavily_image_search_message(response: TavilySearchResponse, *, max_items: int = 3) -> str:
    images = list(response.images or [])
    if not images:
        return f"No image results found for: {response.query}"
    lines = [
        f"Tavily image results for: {response.query}",
        "Use one direct image URL below if the user wants to see what it looks like.",
    ]
    for index, image_url in enumerate(images[:max_items], start=1):
        lines.append(f"{index}. {image_url}")
    return "\n".join(lines)


def format_tavily_extract_message(response: TavilyExtractResponse, *, max_chars: int = 1800) -> str:
    if not response.results:
        return f"No extractable content found for: {response.url}"
    result = response.results[0]
    content = " ".join(result.raw_content.split())
    if len(content) > max_chars:
        content = f"{content[: max_chars - 3].rstrip()}..."
    lines = [f"Tavily extract for: {result.url}"]
    if result.title:
        lines.append(f"Title: {result.title}")
    if response.query:
        lines.append(f"Focus: {response.query}")
    lines.append(content)
    return "\n".join(lines)
