"""Bounded on-demand HTTP reads for supported public market pages."""
import asyncio
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from nycti.http_tls import http_ssl_context
from nycti.chat.tools.market_pages import market_page_info
from nycti.chat.tools.market_page_data import MarketPageDataError, MarketPageObservation, parse_market_page
from nycti.yahoo.quote_cache import QuotePageCache


USER_AGENT = "NyctiMarketAgent/1.0 (on-demand public market-page agent)"
MAX_PAGE_BYTES = 1_000_000


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketPageDataError("Market page redirected; use normal source discovery")


class MarketPageClient:
    def __init__(self, *, fetch_text=None, timeout_seconds=5.0):
        self.timeout_seconds = timeout_seconds
        self._fetch_text = fetch_text or self._fetch_text_sync
        # Reuse the existing 15-second, bounded, cancellation-safe single-flight cache.
        # Only parsed observations are retained; raw HTML is discarded.
        self._cache = QuotePageCache()
        self._semaphore = asyncio.Semaphore(2)

    async def get(self, url: str) -> MarketPageObservation:
        if market_page_info(url) is None or urlsplit(url).query or urlsplit(url).fragment:
            raise MarketPageDataError("Unsupported direct market URL")

        async def load():
            async with self._semaphore:
                async with asyncio.timeout(self.timeout_seconds * 2 + 1):
                    html = await asyncio.to_thread(self._permitted_page, url)
                observation = parse_market_page(url, html)
                return {"observation": observation}

        value = (await self._cache.get(url, load))["observation"]
        if not isinstance(value, MarketPageObservation):
            raise MarketPageDataError("Unexpected cached market record")
        return value

    def _permitted_page(self, url):
        # Check the current policy on each uncached lookup. Two bounded requests,
        # no cookies/login, redirects, private endpoints, or anti-bot workarounds.
        robots_url = "https://" + urlsplit(url).hostname + "/robots.txt"
        policy = RobotFileParser(robots_url)
        try:
            robots = self._fetch_text(robots_url, 64000)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            robots = "User-agent: *\nAllow: /"
        policy.parse(robots.splitlines())
        if not policy.can_fetch(USER_AGENT, url):
            raise MarketPageDataError("Direct market lookup disallowed by robots policy")
        return self._fetch_text(url, MAX_PAGE_BYTES)

    def _fetch_text_sync(self, url, limit):
        opener = build_opener(_NoRedirects(), HTTPSHandler(context=http_ssl_context()))
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
        with opener.open(request, timeout=self.timeout_seconds) as response:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise MarketPageDataError("Market response exceeds size limit")
            kind = response.headers.get_content_type()
            if kind not in {"text/html", "text/plain"}:
                raise MarketPageDataError("Unexpected market response type")
            return raw.decode(response.headers.get_content_charset() or "utf-8")


def render_market_observation(observation: MarketPageObservation, notes: str) -> str:
    text = observation.render() + "\nMarket-page interpretation: " + notes
    newest = observation.extended or observation.regular
    if newest is not None:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(newest.timestamp)).total_seconds()
        if age > 900:
            text += "\nThe newest source print is over 15 minutes old; this is last-recorded data, not a confirmed live price."
    if observation.reported_ath is not None:
        text += "\nHistory rows are a bounded visible-page sample, not full coverage or a verified listing-lifetime maximum."
    return text
