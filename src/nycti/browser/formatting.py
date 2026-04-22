from __future__ import annotations

from nycti.browser.models import BrowserExtractResult


def format_browser_extract_message(result: BrowserExtractResult, *, max_chars: int = 1600) -> str:
    lines = [f"Browser extract for: {result.requested_url}"]
    if result.final_url and result.final_url != result.requested_url:
        lines.append(f"Final URL: {result.final_url}")
    if result.title:
        lines.append(f"Title: {result.title}")
    content = result.content.strip()
    if content:
        if len(content) > max_chars:
            content = content[: max_chars - 3].rstrip() + "..."
        lines.append("Content:")
        lines.append(content)
    else:
        lines.append("Content: (empty)")
    return "\n".join(lines)
