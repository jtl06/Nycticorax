from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RSSItem:
    identity: str
    title: str
    link: str
    summary: str = ""
    published: str = ""


@dataclass(frozen=True, slots=True)
class RSSFeed:
    url: str
    title: str
    items: list[RSSItem]
