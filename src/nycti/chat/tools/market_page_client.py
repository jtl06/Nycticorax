"""Bounded on-demand HTTP reads for supported public market pages."""
import asyncio
from datetime import datetime, timezone
import logging
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
ROBOTS_TTL_SECONDS = 86400
MARKET_HOSTS = ("stockscan.io", "stockanalysis.com")
LOGGER = logging.getLogger(__name__)


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
        self._robots_cache = QuotePageCache(ttl_seconds=ROBOTS_TTL_SECONDS)
        self._semaphore = asyncio.Semaphore(2)

    async def warmup(self) -> None:
        """Fetch only the two public policies; never prefetch stock pages."""
        results = await asyncio.gather(*(self._policy(host) for host in MARKET_HOSTS), return_exceptions=True)
        for host, result in zip(MARKET_HOSTS, results):
            if isinstance(result, Exception):
                LOGGER.info("Market robots warmup unavailable for %s: %s", host, type(result).__name__)

    async def _policy(self, host: str) -> RobotFileParser:
        if host not in MARKET_HOSTS:
            raise MarketPageDataError("Unsupported robots host")

        async def load():
            async with asyncio.timeout(self.timeout_seconds + 1):
                policy = await asyncio.to_thread(self._fetch_policy, host)
            return {"policy": policy}

        value = (await self._robots_cache.get(host, load))["policy"]
        if not isinstance(value, RobotFileParser):
            raise MarketPageDataError("Unexpected robots policy")
        return value

    async def get(self, url: str) -> MarketPageObservation:
        if market_page_info(url) is None or urlsplit(url).query or urlsplit(url).fragment:
            raise MarketPageDataError("Unsupported direct market URL")

        async def load():
            policy = await self._policy(urlsplit(url).hostname)
            if not policy.can_fetch(USER_AGENT, url):
                raise MarketPageDataError("Direct market lookup disallowed by robots policy")
            async with self._semaphore:
                async with asyncio.timeout(self.timeout_seconds + 1):
                    html = await asyncio.to_thread(self._fetch_text, url, MAX_PAGE_BYTES)
                observation = parse_market_page(url, html)
                return {"observation": observation}

        value = (await self._cache.get(url, load))["observation"]
        if not isinstance(value, MarketPageObservation):
            raise MarketPageDataError("Unexpected cached market record")
        return value

    def _fetch_policy(self, host):
        # Failed refreshes are not cached, and expired permissions are never reused.
        robots_url = "https://" + host + "/robots.txt"
        policy = RobotFileParser(robots_url)
        try:
            robots = self._fetch_text(robots_url, 64000)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            robots = "User-agent: *\nAllow: /"
        policy.parse(robots.splitlines())
        return policy

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
