from __future__ import annotations

import re
from typing import Any

from nycti.browser.models import (
    BrowserExtractResult,
    BrowserToolDataError,
    BrowserToolDisabledError,
    BrowserToolRuntimeError,
    BrowserToolUnavailableError,
)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class BrowserClient:
    def __init__(
        self,
        *,
        enabled: bool,
        timeout_seconds: float,
        headless: bool,
        allow_headed: bool,
        max_content_chars: int = 6000,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.allow_headed = allow_headed
        self.max_content_chars = max_content_chars

    async def extract(
        self,
        *,
        url: str,
        query: str | None = None,
        headed: bool = False,
    ) -> BrowserExtractResult:
        normalized_url = url.strip()
        if not normalized_url:
            raise BrowserToolDataError("Browser extract URL cannot be empty.")
        if not self.enabled:
            raise BrowserToolDisabledError("Browser extraction is disabled by configuration.")
        if headed and not self.allow_headed:
            raise BrowserToolDisabledError(
                "Headed browser mode is disabled. Set BROWSER_TOOL_ALLOW_HEADED=true to allow it."
            )

        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency/runtime guard
            raise BrowserToolUnavailableError(
                "Playwright is not installed. Install `playwright` and run `playwright install chromium`."
            ) from exc

        launch_headless = self.headless if not headed else False
        timeout_ms = max(int(self.timeout_seconds * 1000), 1000)

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=launch_headless)
                context = await browser.new_context(user_agent=DEFAULT_BROWSER_USER_AGENT)
                page = await context.new_page()
                await page.goto(normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
                except PlaywrightTimeoutError:
                    # Some sites keep background requests open; domcontentloaded is enough.
                    pass
                final_url = (page.url or normalized_url).strip()
                title = " ".join((await page.title()).split()).strip()
                extracted_text = await page.evaluate(_EXTRACTION_SCRIPT)
                await context.close()
                await browser.close()
        except PlaywrightTimeoutError as exc:
            raise BrowserToolRuntimeError(
                f"Browser extract for `{normalized_url}` timed out before the page finished loading."
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive provider/runtime guard
            raise BrowserToolRuntimeError(
                f"Browser extract for `{normalized_url}` failed due to a Chromium runtime error."
            ) from exc

        content = _normalize_content(extracted_text)
        if query:
            content = _focus_content(content, query)
        content = content[: self.max_content_chars].strip()
        if not content and not title:
            raise BrowserToolDataError(
                f"Browser extract for `{normalized_url}` loaded, but no readable content was found."
            )

        return BrowserExtractResult(
            requested_url=normalized_url,
            final_url=final_url or normalized_url,
            title=title,
            content=content,
        )


def _normalize_content(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _focus_content(content: str, query: str) -> str:
    normalized_query = " ".join(query.split()).strip().lower()
    if not normalized_query:
        return content
    query_terms = [term for term in re.split(r"[^a-z0-9]+", normalized_query) if len(term) >= 3]
    if not query_terms:
        return content
    selected_lines: list[str] = []
    for line in content.splitlines():
        normalized_line = line.lower()
        if any(term in normalized_line for term in query_terms):
            selected_lines.append(line.strip())
        if len(selected_lines) >= 20:
            break
    if not selected_lines:
        return content
    return "\n".join(selected_lines)


_EXTRACTION_SCRIPT = """
() => {
  const candidates = [
    "article",
    "main",
    "[role='main']",
    ".article-body",
    ".post-content",
    ".story-body",
    ".news-release",
    ".newsrelease-body",
    ".content"
  ];
  let root = null;
  for (const selector of candidates) {
    const el = document.querySelector(selector);
    if (el && el.innerText && el.innerText.trim().length > 120) {
      root = el;
      break;
    }
  }
  if (!root) {
    root = document.body;
  }
  const text = (root && root.innerText ? root.innerText : "").trim();
  const metaDescription = (document.querySelector("meta[name='description']") || {}).content || "";
  const heading = ((document.querySelector("h1") || {}).innerText || "").trim();
  const parts = [];
  if (heading) parts.push(heading);
  if (metaDescription) parts.push(metaDescription);
  if (text) parts.push(text);
  return parts.join("\\n\\n").trim();
}
"""
