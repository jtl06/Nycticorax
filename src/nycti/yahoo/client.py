from __future__ import annotations

import asyncio
import gzip
import html
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nycti.http_tls import urlopen
from nycti.yahoo.annual import parse_annual_performance
from nycti.yahoo.models import (
    YahooAnnualPerformance,
    YahooExtendedHoursQuote,
    YahooMarketSnapshot,
)

YAHOO_FINANCE_BASE_URL = "https://query2.finance.yahoo.com"
YAHOO_FINANCE_PAGE_BASE_URL = "https://finance.yahoo.com"
YAHOO_FINANCE_USER_AGENT = "Mozilla/5.0"
EXTENDED_MARKET_STATES = {"PRE", "PREPRE", "POST", "POSTPOST"}
DEFAULT_EXCHANGE_TIMEZONE = "America/New_York"


class YahooFinanceClient:
    def __init__(
        self,
        *,
        base_url: str = YAHOO_FINANCE_BASE_URL,
        page_base_url: str = YAHOO_FINANCE_PAGE_BASE_URL,
        timeout_seconds: float = 8.0,
        fetch_json: Callable[[str], object] | None = None,
        fetch_text: Callable[[str], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_base_url = page_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._fetch_json_sync
        self._fetch_text = fetch_text or (self._fetch_text_sync if fetch_json is None else None)
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def get_extended_hours_quote(self, symbol: str) -> YahooExtendedHoursQuote:
        normalized_symbol = _normalize_symbol(symbol)
        if self._fetch_text is not None:
            try:
                page_text = await asyncio.to_thread(self._fetch_text, self._quote_page_url(normalized_symbol))
                return _parse_quote_page_extended_hours_quote(normalized_symbol, page_text, now=self._now())
            except (YahooFinanceDataError, YahooFinanceHTTPError, YahooFinanceNoExtendedHoursError):
                pass
        params = {
            "range": "1d",
            "interval": "1m",
            "includePrePost": "true",
            "events": "div,splits",
        }
        url = f"{self.base_url}/v8/finance/chart/{quote_plus(normalized_symbol)}?{urlencode(params)}"
        payload = await asyncio.to_thread(self._fetch_json, url)
        if not isinstance(payload, Mapping):
            raise YahooFinanceDataError("Yahoo Finance chart response had an unexpected shape.")
        return _parse_extended_hours_quote(normalized_symbol, payload)

    async def get_market_snapshot(self, symbol: str) -> YahooMarketSnapshot:
        normalized_symbol = _normalize_symbol(symbol)
        if self._fetch_text is None:
            raise YahooFinanceDataError("Yahoo Finance page fetching is not configured.")
        page_text = await asyncio.to_thread(
            self._fetch_text,
            self._quote_page_url(normalized_symbol),
        )
        return _parse_quote_page_market_snapshot(
            normalized_symbol,
            page_text,
            now=self._now(),
        )

    async def get_annual_performance(
        self,
        symbol: str,
        *,
        start_year: int,
    ) -> YahooAnnualPerformance:
        requested_symbol = _normalize_symbol(symbol)
        normalized_symbol = {"SPX": "^GSPC"}.get(requested_symbol, requested_symbol)
        now = self._now()
        period_start = datetime(start_year - 1, 12, 1, tzinfo=timezone.utc)
        params = {
            "period1": int(period_start.timestamp()),
            "period2": int((now + timedelta(days=1)).timestamp()),
            "interval": "1d",
            "events": "div",
            "includeAdjustedClose": "true",
        }
        url = f"{self.base_url}/v8/finance/chart/{quote_plus(normalized_symbol)}?{urlencode(params)}"
        payload = await asyncio.to_thread(self._fetch_json, url)
        if not isinstance(payload, Mapping):
            raise YahooFinanceDataError("Yahoo Finance chart response had an unexpected shape.")
        try:
            return parse_annual_performance(
                requested_symbol,
                payload,
                start_year=start_year,
                now=now,
            )
        except ValueError as exc:
            raise YahooFinanceDataError(str(exc)) from exc

    def _quote_page_url(self, symbol: str) -> str:
        return f"{self.page_base_url}/quote/{quote_plus(symbol)}/"

    def _fetch_json_sync(self, url: str) -> object:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": YAHOO_FINANCE_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = detail or f"Yahoo Finance request failed with HTTP {exc.code}."
            raise YahooFinanceHTTPError(message) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise YahooFinanceHTTPError(f"Yahoo Finance request failed: {reason}.") from exc
        if not raw:
            raise YahooFinanceDataError("Yahoo Finance response was empty.")
        try:
            text = raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise YahooFinanceDataError("Yahoo Finance response was not valid text.") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YahooFinanceDataError("Yahoo Finance response was not valid JSON.") from exc

    def _fetch_text_sync(self, url: str) -> str:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
                "User-Agent": YAHOO_FINANCE_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = detail or f"Yahoo Finance page request failed with HTTP {exc.code}."
            raise YahooFinanceHTTPError(message) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise YahooFinanceHTTPError(f"Yahoo Finance page request failed: {reason}.") from exc
        if not raw:
            raise YahooFinanceDataError("Yahoo Finance page response was empty.")
        if encoding.casefold() == "gzip":
            raw = gzip.decompress(raw)
        try:
            return raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise YahooFinanceDataError("Yahoo Finance page response was not valid text.") from exc


def _parse_quote_page_extended_hours_quote(
    symbol: str,
    page_text: str,
    *,
    now: datetime | None = None,
) -> YahooExtendedHoursQuote:
    snapshot = _parse_quote_page_market_snapshot(symbol, page_text, now=now)
    if (
        snapshot.extended_session is not None
        and snapshot.extended_price is not None
        and snapshot.extended_timestamp is not None
    ):
        return YahooExtendedHoursQuote(
            symbol=snapshot.symbol,
            price=snapshot.extended_price,
            timestamp=snapshot.extended_timestamp,
            session=snapshot.extended_session,
            currency=snapshot.currency,
            exchange_name=snapshot.exchange_name,
            timezone_name=snapshot.timezone_name,
            market_state=snapshot.market_state,
            regular_price=snapshot.regular_price,
            regular_previous_close=snapshot.regular_previous_close,
            regular_change=snapshot.regular_change,
            regular_percent_change=snapshot.regular_percent_change,
            regular_timestamp=snapshot.regular_timestamp,
        )
    raise YahooFinanceNoExtendedHoursError("Yahoo Finance page did not return a current 24h/pre/post-market price.")


def _parse_quote_page_market_snapshot(
    symbol: str,
    page_text: str,
    *,
    now: datetime | None = None,
) -> YahooMarketSnapshot:
    result = _quote_page_result(symbol, page_text)
    market_state = _clean_optional_text(result.get("marketState"))
    candidates = _quote_page_candidates(result)
    preferred_sessions = _preferred_quote_page_sessions_for_time(result, now)
    if preferred_sessions is None:
        preferred_sessions = _preferred_quote_page_sessions_for_market_state(market_state)
    if preferred_sessions is not None:
        candidates = [
            candidate for candidate in candidates if candidate[0] in preferred_sessions
        ]
    selected = max(candidates, key=lambda candidate: candidate[2]) if candidates else None
    return YahooMarketSnapshot(
        symbol=str(result.get("symbol", "")).strip() or symbol,
        currency=_clean_optional_text(result.get("currency")),
        exchange_name=_clean_optional_text(
            result.get("fullExchangeName") or result.get("exchange")
        ),
        timezone_name=_clean_optional_text(result.get("exchangeTimezoneName")),
        market_state=market_state,
        regular_price=_coerce_quote_float(result.get("regularMarketPrice")),
        regular_previous_close=_coerce_quote_float(
            result.get("regularMarketPreviousClose")
        ),
        regular_change=_coerce_quote_float(result.get("regularMarketChange")),
        regular_percent_change=_coerce_quote_float(
            result.get("regularMarketChangePercent")
        ),
        regular_timestamp=_coerce_quote_int(result.get("regularMarketTime")),
        market_cap=_coerce_quote_float(result.get("marketCap")),
        shares_outstanding=_coerce_quote_float(result.get("sharesOutstanding")),
        implied_shares_outstanding=_coerce_quote_float(
            result.get("impliedSharesOutstanding")
        ),
        extended_session=selected[0] if selected else None,
        extended_price=selected[1] if selected else None,
        extended_timestamp=selected[2] if selected else None,
    )


def _quote_page_candidates(result: Mapping[str, object]) -> list[tuple[str, float, int]]:
    fields = (
        ("overnight", "overnightMarketPrice", "overnightMarketTime"),
        ("pre", "preMarketPrice", "preMarketTime"),
        ("post", "postMarketPrice", "postMarketTime"),
    )
    candidates: list[tuple[str, float, int]] = []
    for session, price_key, time_key in fields:
        price = _coerce_quote_float(result.get(price_key))
        timestamp = _coerce_quote_int(result.get(time_key))
        if price is not None and timestamp is not None:
            candidates.append((session, price, timestamp))
    return candidates


def _preferred_quote_page_sessions_for_time(
    result: Mapping[str, object],
    now: datetime | None,
) -> set[str] | None:
    if now is None:
        return None
    timezone_name = _clean_optional_text(result.get("exchangeTimezoneName")) or DEFAULT_EXCHANGE_TIMEZONE
    try:
        exchange_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        exchange_timezone = ZoneInfo(DEFAULT_EXCHANGE_TIMEZONE)
    local_now = now.astimezone(exchange_timezone) if now.tzinfo else now.replace(tzinfo=exchange_timezone)
    current_time = local_now.time()
    weekday = local_now.weekday()
    if time(4, 0) <= current_time < time(9, 30) and weekday < 5:
        return {"pre"}
    if time(9, 30) <= current_time < time(16, 0) and weekday < 5:
        return set()
    if time(16, 0) <= current_time < time(20, 0) and weekday < 5:
        return {"post"}
    if current_time >= time(20, 0):
        return {"overnight"} if weekday in {0, 1, 2, 3, 6} else set()
    if current_time < time(4, 0):
        return {"overnight"} if weekday < 5 else set()
    return set()


def _preferred_quote_page_sessions_for_market_state(market_state: str | None) -> set[str] | None:
    if market_state is None:
        return None
    normalized = market_state.upper()
    if normalized == "OVERNIGHT":
        return {"overnight"}
    if normalized.startswith("PRE"):
        return {"pre"}
    if normalized.startswith("POST"):
        return {"post"}
    return None


def _quote_page_result(symbol: str, page_text: str) -> Mapping[str, object]:
    symbol_variants = {symbol, quote_plus(symbol)}
    pattern = r'<script type="application/json"[^>]*data-url="(?P<url>[^"]*)"[^>]*>(?P<body>.*?)</script>'
    for match in re.finditer(pattern, page_text, flags=re.DOTALL):
        data_url = html.unescape(match.group("url"))
        if "/v7/finance/quote" not in data_url or not any(f"symbols={variant}" in data_url for variant in symbol_variants):
            continue
        try:
            outer = json.loads(html.unescape(match.group("body")))
            body = outer.get("body")
            payload = json.loads(body) if isinstance(body, str) else body
            return _quote_response_result(payload)
        except YahooFinanceDataError:
            continue
        except (json.JSONDecodeError, TypeError):
            continue
    raise YahooFinanceDataError("Yahoo Finance page did not include usable quote data.")


def _quote_response_result(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise YahooFinanceDataError("Yahoo Finance page quote data had an unexpected shape.")
    quote_response = payload.get("quoteResponse")
    if not isinstance(quote_response, Mapping):
        raise YahooFinanceDataError("Yahoo Finance page quote data did not include quoteResponse.")
    results = quote_response.get("result")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
        raise YahooFinanceDataError("Yahoo Finance page quote data did not include a result.")
    result = results[0]
    if not isinstance(result, Mapping):
        raise YahooFinanceDataError("Yahoo Finance page quote result had an unexpected shape.")
    return result


def _coerce_quote_float(value: object) -> float | None:
    if isinstance(value, Mapping):
        return _coerce_float(value.get("raw"))
    return _coerce_float(value)


def _coerce_quote_int(value: object) -> int | None:
    if isinstance(value, Mapping):
        return _coerce_int(value.get("raw"))
    return _coerce_int(value)


def _parse_extended_hours_quote(symbol: str, payload: Mapping[str, object]) -> YahooExtendedHoursQuote:
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise YahooFinanceDataError("Yahoo Finance chart response did not include chart data.")
    error = chart.get("error")
    if error:
        raise YahooFinanceHTTPError(str(error))
    results = chart.get("result")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
        raise YahooFinanceDataError("Yahoo Finance chart response did not include a result.")
    result = results[0]
    if not isinstance(result, Mapping):
        raise YahooFinanceDataError("Yahoo Finance chart result had an unexpected shape.")
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        raise YahooFinanceDataError("Yahoo Finance chart result did not include metadata.")

    market_state = _clean_optional_text(meta.get("marketState"))
    timestamp, price = _latest_timestamped_close(result)
    session = _extended_session_for_market_state(market_state)
    if session is None and market_state and market_state.upper() not in EXTENDED_MARKET_STATES:
        raise YahooFinanceNoExtendedHoursError(
            f"Yahoo Finance market state is {market_state}, not an active extended-hours session."
        )
    if session is None:
        session = _extended_session_for_trading_period(timestamp=timestamp, meta=meta)
    if session is None:
        raise YahooFinanceNoExtendedHoursError("Yahoo Finance did not return a current pre/post-market price.")
    return YahooExtendedHoursQuote(
        symbol=str(meta.get("symbol", "")).strip() or symbol,
        price=price,
        timestamp=timestamp,
        session=session,
        currency=_clean_optional_text(meta.get("currency")),
        exchange_name=_clean_optional_text(meta.get("exchangeName") or meta.get("fullExchangeName")),
        timezone_name=_clean_optional_text(meta.get("exchangeTimezoneName")),
        market_state=market_state,
        regular_price=_coerce_float(meta.get("regularMarketPrice")),
        regular_previous_close=_coerce_float(
            meta.get("previousClose") or meta.get("chartPreviousClose")
        ),
        regular_timestamp=_coerce_int(meta.get("regularMarketTime")),
    )


def _regular_session_bounds(meta: Mapping[str, object]) -> tuple[int | None, int | None]:
    period = meta.get("currentTradingPeriod")
    if not isinstance(period, Mapping):
        return None, None
    regular = period.get("regular")
    if not isinstance(regular, Mapping):
        return None, None
    return _coerce_int(regular.get("start")), _coerce_int(regular.get("end"))


def _trading_period_bounds(meta: Mapping[str, object], name: str) -> tuple[int | None, int | None]:
    period = meta.get("currentTradingPeriod")
    if not isinstance(period, Mapping):
        return None, None
    segment = period.get(name)
    if not isinstance(segment, Mapping):
        return None, None
    return _coerce_int(segment.get("start")), _coerce_int(segment.get("end"))


def _latest_timestamped_close(result: Mapping[str, object]) -> tuple[int, float]:
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, Sequence) or isinstance(timestamps, (str, bytes)):
        raise YahooFinanceDataError("Yahoo Finance chart result did not include timestamps.")
    closes = _close_values(result)
    for raw_timestamp, raw_close in reversed(list(zip(timestamps, closes))):
        timestamp = _coerce_int(raw_timestamp)
        close = _coerce_float(raw_close)
        if timestamp is not None and close is not None:
            return timestamp, close
    raise YahooFinanceNoExtendedHoursError("Yahoo Finance did not return a usable latest price.")


def _close_values(result: Mapping[str, object]) -> Sequence[object]:
    indicators = result.get("indicators")
    if not isinstance(indicators, Mapping):
        raise YahooFinanceDataError("Yahoo Finance chart result did not include indicators.")
    quotes = indicators.get("quote")
    if not isinstance(quotes, Sequence) or isinstance(quotes, (str, bytes)) or not quotes:
        raise YahooFinanceDataError("Yahoo Finance chart result did not include quote values.")
    quote_values = quotes[0]
    if not isinstance(quote_values, Mapping):
        raise YahooFinanceDataError("Yahoo Finance quote values had an unexpected shape.")
    closes = quote_values.get("close")
    if not isinstance(closes, Sequence) or isinstance(closes, (str, bytes)):
        raise YahooFinanceDataError("Yahoo Finance quote values did not include closes.")
    return closes


def _extended_session_for_trading_period(
    *,
    timestamp: int,
    meta: Mapping[str, object],
) -> str | None:
    pre_start, pre_end = _trading_period_bounds(meta, "pre")
    if pre_start is not None and pre_end is not None and pre_start <= timestamp <= pre_end:
        return "pre"
    post_start, post_end = _trading_period_bounds(meta, "post")
    if post_start is not None and post_end is not None and post_start <= timestamp <= post_end:
        return "post"
    regular_start, regular_end = _regular_session_bounds(meta)
    if pre_start is None and regular_start is not None and timestamp < regular_start:
        return "pre"
    if post_end is None and regular_end is not None and timestamp > regular_end:
        return "post"
    return None


def _extended_session_for_market_state(market_state: str | None) -> str | None:
    if market_state is None:
        return None
    normalized = market_state.upper()
    if normalized.startswith("PRE"):
        return "pre"
    if normalized.startswith("POST"):
        return "post"
    return None


def _normalize_symbol(symbol: str) -> str:
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise YahooFinanceDataError("Symbol cannot be empty.")
    return normalized_symbol


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


class YahooFinanceError(Exception):
    pass


class YahooFinanceHTTPError(YahooFinanceError):
    pass


class YahooFinanceDataError(YahooFinanceError):
    pass


class YahooFinanceNoExtendedHoursError(YahooFinanceError):
    pass
