from __future__ import annotations

import asyncio
import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from nycti.rss.models import RSSFeed, RSSItem


class RSSFetchError(RuntimeError):
    pass


class RSSParseError(RuntimeError):
    pass


class RSSClient:
    def __init__(self, *, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds

    async def fetch_feed(self, url: str) -> RSSFeed:
        payload = await asyncio.to_thread(self._fetch_url, url)
        return parse_rss_feed(payload, feed_url=url)

    def _fetch_url(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "NyctiRSS/0.1 (+https://github.com/jtl06/Nycticorax)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read(2_000_000)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RSSFetchError(str(exc)) from exc


def parse_rss_feed(payload: bytes | str, *, feed_url: str) -> RSSFeed:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RSSParseError(str(exc)) from exc

    if _tag_name(root.tag) == "rss" or root.find("channel") is not None:
        return _parse_rss(root, feed_url=feed_url)
    if _tag_name(root.tag) == "feed":
        return _parse_atom(root, feed_url=feed_url)
    raise RSSParseError("Unsupported RSS/Atom feed format.")


def _parse_rss(root: ET.Element, *, feed_url: str) -> RSSFeed:
    channel = root.find("channel") or root
    feed_title = _child_text(channel, "title") or feed_url
    items: list[RSSItem] = []
    for item in channel.findall("item"):
        title = _normalize_text(_child_text(item, "title")) or "(untitled)"
        link = _normalize_text(_child_text(item, "link"))
        guid = _normalize_text(_child_text(item, "guid"))
        summary = _normalize_text(_child_text(item, "description"))
        published = _normalize_text(_child_text(item, "pubDate"))
        identity = guid or link or f"{title}:{published}"
        if identity:
            items.append(
                RSSItem(
                    identity=identity,
                    title=title,
                    link=link,
                    summary=summary,
                    published=published,
                )
            )
    return RSSFeed(url=feed_url, title=_normalize_text(feed_title), items=items)


def _parse_atom(root: ET.Element, *, feed_url: str) -> RSSFeed:
    feed_title = _child_text(root, "title") or feed_url
    items: list[RSSItem] = []
    for entry in [child for child in root if _tag_name(child.tag) == "entry"]:
        title = _normalize_text(_child_text(entry, "title")) or "(untitled)"
        link = _atom_link(entry)
        entry_id = _normalize_text(_child_text(entry, "id"))
        summary = _normalize_text(_child_text(entry, "summary") or _child_text(entry, "content"))
        published = _normalize_text(_child_text(entry, "published") or _child_text(entry, "updated"))
        identity = entry_id or link or f"{title}:{published}"
        if identity:
            items.append(
                RSSItem(
                    identity=identity,
                    title=title,
                    link=link,
                    summary=summary,
                    published=published,
                )
            )
    return RSSFeed(url=feed_url, title=_normalize_text(feed_title), items=items)


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in entry:
        if _tag_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href", "")).strip()
        if not href:
            continue
        if child.attrib.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _child_text(element: ET.Element, child_name: str) -> str:
    for child in element:
        if _tag_name(child.tag) == child_name:
            return "".join(child.itertext()).strip()
    return ""


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_text(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()
