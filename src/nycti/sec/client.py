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
SEC_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "call",
    "earnings",
    "er",
    "filing",
    "filings",
    "for",
    "from",
    "in",
    "inc",
    "is",
    "latest",
    "me",
    "of",
    "on",
    "or",
    "q",
    "quarter",
    "report",
    "sec",
    "show",
    "the",
    "to",
    "use",
    "what",
}
COMPANY_NAME_STOPWORDS = SEC_QUERY_STOPWORDS | {
    "ag",
    "corp",
    "corporation",
    "group",
    "holdings",
    "incorporated",
    "limited",
    "ltd",
    "plc",
    "sa",
    "technologies",
}


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
        self._company_tokens_cache: dict[str, set[str]] | None = None

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
        companies = await self.find_matching_companies(text, limit=1)
        if companies:
            return await self.latest_filings(companies[0].ticker, limit=limit)
        raise SecTickerNotFoundError("No valid ticker was found in the SEC query.")

    async def find_matching_companies(self, text: str, *, limit: int = 8) -> list[SecCompanyRecord]:
        companies = await self._load_company_records()
        matches: list[SecCompanyRecord] = []
        seen: set[str] = set()

        for candidate in self._extract_ticker_candidates(text):
            company = companies.get(candidate)
            if company is None or company.ticker in seen:
                continue
            matches.append(company)
            seen.add(company.ticker)
            if len(matches) >= limit:
                return matches

        company_matches = self._rank_company_name_matches(text, companies)
        for company in company_matches:
            if company.ticker in seen:
                continue
            matches.append(company)
            seen.add(company.ticker)
            if len(matches) >= limit:
                break
        return matches

    async def _load_company_records(self) -> dict[str, SecCompanyRecord]:
        if self._ticker_cache is not None:
            return self._ticker_cache
        payload = await self._fetch_json_async(COMPANY_TICKERS_URL)
        records = parse_company_tickers(payload)
        if not records:
            raise SecDataError("SEC ticker map was empty or invalid.")
        self._ticker_cache = records
        self._company_tokens_cache = None
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
            if ticker.lower() in SEC_QUERY_STOPWORDS:
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            candidates.append(ticker)
        return candidates

    def _rank_company_name_matches(
        self,
        text: str,
        companies: Mapping[str, SecCompanyRecord],
    ) -> list[SecCompanyRecord]:
        query_tokens = self._tokenize_company_text(text, stopwords=SEC_QUERY_STOPWORDS)
        if not query_tokens:
            return []

        ranked: list[tuple[int, SecCompanyRecord]] = []
        company_tokens_cache = self._load_company_tokens(companies)
        for ticker, company in companies.items():
            company_tokens = company_tokens_cache.get(ticker, set())
            if not company_tokens:
                continue
            overlap = query_tokens & company_tokens
            score = len(overlap)
            if score <= 0:
                continue
            ranked.append((score, company))
        ranked.sort(key=lambda item: (-item[0], item[1].company_name))
        return [company for _, company in ranked]

    def _load_company_tokens(
        self,
        companies: Mapping[str, SecCompanyRecord],
    ) -> dict[str, set[str]]:
        if self._company_tokens_cache is not None:
            return self._company_tokens_cache
        self._company_tokens_cache = {
            ticker: self._tokenize_company_text(record.company_name, stopwords=COMPANY_NAME_STOPWORDS)
            for ticker, record in companies.items()
        }
        return self._company_tokens_cache

    def _tokenize_company_text(self, text: str, *, stopwords: set[str]) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]{2,}", text.lower())
            if token not in stopwords
        }

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
