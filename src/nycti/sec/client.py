from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nycti.sec.models import (
    SecCompanyRecord,
    SecDataError,
    SecHTTPError,
    SecLatestFilings,
    SecNoFilingsError,
    SecTickerNotFoundError,
    SecUserAgentMissingError,
)
from nycti.sec.parser import normalize_ticker, parse_company_tickers, parse_recent_filings


COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class SecClient:
    def __init__(
        self,
        user_agent: str | None,
        *,
        timeout_seconds: float = 10.0,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.user_agent = user_agent.strip() if user_agent and user_agent.strip() else None
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._fetch_json_sync
        self._ticker_cache: dict[str, SecCompanyRecord] | None = None

    async def latest_filings(self, ticker: str, *, limit: int = 5) -> SecLatestFilings:
        if self.user_agent is None:
            raise SecUserAgentMissingError(
                "SEC_USER_AGENT is not configured. Set it to a contact-style user agent before using /sec_latest."
            )

        normalized_ticker = normalize_ticker(ticker)
        if not normalized_ticker:
            raise SecTickerNotFoundError("Ticker cannot be empty.")

        companies = await self._load_company_records()
        company = companies.get(normalized_ticker)
        if company is None:
            raise SecTickerNotFoundError(f"Unknown SEC ticker: {normalized_ticker}")

        submissions_url = SUBMISSIONS_URL_TEMPLATE.format(cik=company.cik)
        submissions_payload = await self._fetch_json_async(submissions_url)
        filings = parse_recent_filings(submissions_payload, cik=company.cik, limit=limit)
        if not filings:
            raise SecNoFilingsError(f"No recent SEC filings found for {normalized_ticker}.")

        return SecLatestFilings(
            ticker=company.ticker,
            company_name=company.company_name,
            cik=company.cik,
            filings=filings,
        )

    async def latest_filings_from_text(self, text: str, *, limit: int = 5) -> SecLatestFilings:
        if self.user_agent is None:
            raise SecUserAgentMissingError(
                "SEC_USER_AGENT is not configured. Set it to a contact-style user agent before using SEC search."
            )
        companies = await self._load_company_records()
        candidates = self._extract_ticker_candidates(text)
        for candidate in candidates:
            if candidate in companies:
                return await self.latest_filings(candidate, limit=limit)
        raise SecTickerNotFoundError("No valid ticker was found in the SEC query.")

    async def _load_company_records(self) -> dict[str, SecCompanyRecord]:
        if self._ticker_cache is not None:
            return self._ticker_cache
        payload = await self._fetch_json_async(COMPANY_TICKERS_URL)
        records = parse_company_tickers(payload)
        if not records:
            raise SecDataError("SEC ticker map was empty or invalid.")
        self._ticker_cache = records
        return records

    def _extract_ticker_candidates(self, text: str) -> list[str]:
        normalized = " ".join(text.split())
        if not normalized:
            return []
        tokens = re.findall(r"\b[A-Za-z]{1,5}\b", normalized)
        candidates: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            ticker = normalize_ticker(token)
            if ticker in seen:
                continue
            seen.add(ticker)
            candidates.append(ticker)
        return candidates

    async def _fetch_json_async(self, url: str) -> object:
        return await asyncio.to_thread(self._fetch_json, url)

    def _fetch_json_sync(self, url: str) -> object:
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent or "",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise SecHTTPError(f"SEC request to {url} failed with HTTP {exc.code}.") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise SecHTTPError(f"SEC request to {url} failed: {reason}.") from exc

        if not raw:
            raise SecDataError(f"SEC response from {url} was empty.")

        try:
            text = raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise SecDataError(f"SEC response from {url} was not valid text.") from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SecDataError(f"SEC response from {url} was not valid JSON.") from exc

        if not isinstance(payload, (dict, list)):
            raise SecDataError(f"SEC response from {url} had an unexpected JSON shape.")
        return payload
