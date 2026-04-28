from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from nycti.yahoo.models import YahooExtendedHoursQuote

YAHOO_FINANCE_BASE_URL = "https://query2.finance.yahoo.com"
YAHOO_FINANCE_USER_AGENT = "Mozilla/5.0"


class YahooFinanceClient:
    def __init__(
        self,
        *,
        base_url: str = YAHOO_FINANCE_BASE_URL,
        timeout_seconds: float = 8.0,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._fetch_json_sync

    async def get_extended_hours_quote(self, symbol: str) -> YahooExtendedHoursQuote:
        normalized_symbol = _normalize_symbol(symbol)
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

    regular_start, regular_end = _regular_session_bounds(meta)
    timestamp, price = _latest_timestamped_close(result)
    session = _extended_session_for_timestamp(
        timestamp=timestamp,
        regular_start=regular_start,
        regular_end=regular_end,
    )
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
        market_state=_clean_optional_text(meta.get("marketState")),
    )


def _regular_session_bounds(meta: Mapping[str, object]) -> tuple[int | None, int | None]:
    period = meta.get("currentTradingPeriod")
    if not isinstance(period, Mapping):
        return None, None
    regular = period.get("regular")
    if not isinstance(regular, Mapping):
        return None, None
    return _coerce_int(regular.get("start")), _coerce_int(regular.get("end"))


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


def _extended_session_for_timestamp(
    *,
    timestamp: int,
    regular_start: int | None,
    regular_end: int | None,
) -> str | None:
    if regular_start is not None and timestamp < regular_start:
        return "pre"
    if regular_end is not None and timestamp > regular_end:
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
