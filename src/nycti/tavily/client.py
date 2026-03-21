from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nycti.tavily.models import (
    TavilyAPIKeyMissingError,
    TavilyDataError,
    TavilyExtractResponse,
    TavilyExtractResult,
    TavilyHTTPError,
    TavilySearchResponse,
    TavilySearchResult,
)


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


class TavilyClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        timeout_seconds: float = 10.0,
        post_json: Callable[[str, Mapping[str, object]], object] | None = None,
    ) -> None:
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.timeout_seconds = timeout_seconds
        self._post_json = post_json or self._post_json_sync

    async def search(self, query: str, *, max_results: int = 5) -> TavilySearchResponse:
        return await self._search(query, max_results=max_results, include_images=False)

    async def image_search(self, query: str, *, max_results: int = 5) -> TavilySearchResponse:
        return await self._search(query, max_results=max_results, include_images=True)

    async def _search(
        self,
        query: str,
        *,
        max_results: int,
        include_images: bool,
    ) -> TavilySearchResponse:
        if self.api_key is None:
            raise TavilyAPIKeyMissingError(
                "TAVILY_API_KEY is not configured. Set it before using Tavily tools."
            )
        normalized_query = query.strip()
        if not normalized_query:
            raise TavilyDataError("Search query cannot be empty.")

        payload = {
            "api_key": self.api_key,
            "query": normalized_query,
            "search_depth": "basic",
            "max_results": max(1, min(max_results, 8)),
            "include_answer": False,
            "include_images": include_images,
            "include_raw_content": False,
        }
        response_payload = await asyncio.to_thread(self._post_json, TAVILY_SEARCH_URL, payload)
        return TavilySearchResponse(
            query=normalized_query,
            results=self._parse_results(response_payload),
            images=self._parse_images(response_payload) if include_images else None,
        )

    async def extract(self, url: str, *, query: str | None = None) -> TavilyExtractResponse:
        if self.api_key is None:
            raise TavilyAPIKeyMissingError(
                "TAVILY_API_KEY is not configured. Set it before using Tavily tools."
            )
        normalized_url = url.strip()
        if not normalized_url:
            raise TavilyDataError("Extract URL cannot be empty.")
        normalized_query = query.strip() if query and query.strip() else None
        payload: dict[str, object] = {
            "api_key": self.api_key,
            "urls": [normalized_url],
            "extract_depth": "basic",
            "include_images": False,
        }
        if normalized_query is not None:
            payload["query"] = normalized_query
        response_payload = await asyncio.to_thread(self._post_json, TAVILY_EXTRACT_URL, payload)
        return TavilyExtractResponse(
            url=normalized_url,
            query=normalized_query,
            results=self._parse_extract_results(response_payload),
        )

    def _post_json_sync(self, url: str, payload: Mapping[str, object]) -> object:
        request = Request(
            url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise TavilyHTTPError(f"Tavily request failed with HTTP {exc.code}.") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise TavilyHTTPError(f"Tavily request failed: {reason}.") from exc

        if not raw:
            raise TavilyDataError("Tavily response was empty.")

        try:
            text = raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise TavilyDataError("Tavily response was not valid text.") from exc

        try:
            response_payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TavilyDataError("Tavily response was not valid JSON.") from exc

        if not isinstance(response_payload, Mapping):
            raise TavilyDataError("Tavily response had an unexpected shape.")
        return response_payload

    def _parse_results(self, payload: object) -> list[TavilySearchResult]:
        if not isinstance(payload, Mapping):
            raise TavilyDataError("Tavily response had an unexpected shape.")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return []
        results: list[TavilySearchResult] = []
        for entry in raw_results:
            if not isinstance(entry, Mapping):
                continue
            title = str(entry.get("title", "")).strip()
            url = str(entry.get("url", "")).strip()
            content = str(entry.get("content", "")).strip()
            if not title or not url:
                continue
            score: float | None = None
            raw_score = entry.get("score")
            if isinstance(raw_score, (int, float)):
                score = float(raw_score)
            results.append(TavilySearchResult(title=title, url=url, content=content, score=score))
        return results

    def _parse_extract_results(self, payload: object) -> list[TavilyExtractResult]:
        if not isinstance(payload, Mapping):
            raise TavilyDataError("Tavily response had an unexpected shape.")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return []
        results: list[TavilyExtractResult] = []
        for entry in raw_results:
            if not isinstance(entry, Mapping):
                continue
            url = str(entry.get("url", "")).strip()
            raw_content = str(entry.get("raw_content", "")).strip()
            title = str(entry.get("title", "")).strip()
            if not url or not raw_content:
                continue
            results.append(TavilyExtractResult(url=url, raw_content=raw_content, title=title))
        return results

    def _parse_images(self, payload: object) -> list[str]:
        if not isinstance(payload, Mapping):
            raise TavilyDataError("Tavily response had an unexpected shape.")
        raw_images = payload.get("images")
        if not isinstance(raw_images, list):
            return []
        images: list[str] = []
        seen: set[str] = set()
        for entry in raw_images:
            image_url = ""
            if isinstance(entry, str):
                image_url = entry.strip()
            elif isinstance(entry, Mapping):
                image_url = str(
                    entry.get("url")
                    or entry.get("image_url")
                    or entry.get("src")
                    or ""
                ).strip()
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            images.append(image_url)
        return images
