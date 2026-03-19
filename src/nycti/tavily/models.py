from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TavilySearchResult:
    title: str
    url: str
    content: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class TavilySearchResponse:
    query: str
    results: list[TavilySearchResult]


class TavilyError(Exception):
    pass


class TavilyAPIKeyMissingError(TavilyError):
    pass


class TavilyHTTPError(TavilyError):
    pass


class TavilyDataError(TavilyError):
    pass
