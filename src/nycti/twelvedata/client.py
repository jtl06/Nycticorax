from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from nycti.twelvedata.models import (
    TwelveDataAPIKeyMissingError,
    TwelveDataDataError,
    TwelveDataHTTPError,
    TwelveDataQuote,
    TwelveDataTimeSeries,
    TwelveDataTimeSeriesPoint,
    TwelveDataSymbolMatch,
)

TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
TWELVE_DATA_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class TwelveDataClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = TWELVE_DATA_BASE_URL,
        timeout_seconds: float = 10.0,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._fetch_json_sync

    async def get_market_quote(self, symbol: str) -> TwelveDataQuote:
        normalized_symbol = _normalize_symbol(symbol)
        payload = await self._get_json("/quote", {"symbol": normalized_symbol})
        if not isinstance(payload, Mapping):
            raise TwelveDataDataError("Twelve Data quote response had an unexpected shape.")
        return self._parse_quote(normalized_symbol, payload)

    async def search_symbols(self, symbol: str, *, outputsize: int = 5) -> list[TwelveDataSymbolMatch]:
        normalized_symbol = _normalize_symbol(symbol)
        payload = await self._get_json(
            "/symbol_search",
            {"symbol": normalized_symbol, "outputsize": str(outputsize)},
        )
        if not isinstance(payload, Mapping):
            raise TwelveDataDataError("Twelve Data symbol search response had an unexpected shape.")
        raw_matches = payload.get("data")
        if not isinstance(raw_matches, list):
            return []
        matches: list[TwelveDataSymbolMatch] = []
        for item in raw_matches:
            if not isinstance(item, Mapping):
                continue
            symbol_value = str(item.get("symbol", "")).strip()
            if not symbol_value:
                continue
            matches.append(
                TwelveDataSymbolMatch(
                    symbol=symbol_value,
                    instrument_name=_clean_optional_text(item.get("instrument_name") or item.get("name")),
                    exchange=_clean_optional_text(item.get("exchange")),
                    instrument_type=_clean_optional_text(item.get("instrument_type") or item.get("type")),
                    country=_clean_optional_text(item.get("country")),
                )
            )
        return matches

    async def get_price_history(
        self,
        symbol: str,
        *,
        interval: str = "1day",
        outputsize: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TwelveDataTimeSeries:
        normalized_symbol = _normalize_symbol(symbol)
        normalized_interval = interval.strip() or "1day"
        params = {
            "symbol": normalized_symbol,
            "interval": normalized_interval,
            "outputsize": str(outputsize),
        }
        if start_date and start_date.strip():
            params["start_date"] = start_date.strip()
        if end_date and end_date.strip():
            params["end_date"] = end_date.strip()
        payload = await self._get_json("/time_series", params)
        if not isinstance(payload, Mapping):
            raise TwelveDataDataError("Twelve Data time series response had an unexpected shape.")
        return self._parse_time_series(normalized_symbol, normalized_interval, payload)

    async def _get_json(self, path: str, params: Mapping[str, str]) -> object:
        if self.api_key is None:
            raise TwelveDataAPIKeyMissingError(
                "TWELVE_DATA_API_KEY must be configured before using market quotes."
            )
        query = urlencode({**params, "apikey": self.api_key})
        url = f"{self.base_url}{path}?{query}"
        return await asyncio.to_thread(self._fetch_json, url)

    def _fetch_json_sync(self, url: str) -> object:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": TWELVE_DATA_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = _format_http_error_detail(detail, status_code=exc.code)
            raise TwelveDataHTTPError(message) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise TwelveDataHTTPError(f"Twelve Data request failed: {reason}.") from exc

        if not raw:
            raise TwelveDataDataError("Twelve Data response was empty.")

        try:
            text = raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise TwelveDataDataError("Twelve Data response was not valid text.") from exc

        try:
            response_payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TwelveDataDataError("Twelve Data response was not valid JSON.") from exc

        if isinstance(response_payload, Mapping) and str(response_payload.get("status", "")).lower() == "error":
            detail = str(response_payload.get("message", "Twelve Data request failed."))
            raise TwelveDataHTTPError(_format_http_error_detail(detail))
        return response_payload

    def _parse_quote(self, symbol: str, payload: Mapping[str, object]) -> TwelveDataQuote:
        return TwelveDataQuote(
            symbol=str(payload.get("symbol", "")).strip() or symbol,
            name=_clean_optional_text(payload.get("name")),
            exchange=_clean_optional_text(payload.get("exchange")),
            instrument_type=_clean_optional_text(payload.get("type")),
            currency=_clean_optional_text(payload.get("currency")),
            datetime=_clean_optional_text(payload.get("datetime")),
            close=_coerce_float(payload.get("close")),
            previous_close=_coerce_float(payload.get("previous_close")),
            change=_coerce_float(payload.get("change")),
            percent_change=_coerce_float(payload.get("percent_change")),
            high=_coerce_float(payload.get("high")),
            low=_coerce_float(payload.get("low")),
            open=_coerce_float(payload.get("open")),
            volume=_coerce_int(payload.get("volume")),
            is_market_open=_coerce_bool(payload.get("is_market_open")),
        )

    def _parse_time_series(
        self,
        symbol: str,
        interval: str,
        payload: Mapping[str, object],
    ) -> TwelveDataTimeSeries:
        meta = payload.get("meta")
        meta_mapping = meta if isinstance(meta, Mapping) else {}
        raw_values = payload.get("values")
        if raw_values is None:
            raise TwelveDataDataError("Twelve Data time series response did not include values.")
        if not isinstance(raw_values, list):
            raise TwelveDataDataError("Twelve Data time series values had an unexpected shape.")
        values: list[TwelveDataTimeSeriesPoint] = []
        for item in raw_values:
            if not isinstance(item, Mapping):
                continue
            datetime_value = _clean_optional_text(item.get("datetime"))
            if not datetime_value:
                continue
            values.append(
                TwelveDataTimeSeriesPoint(
                    datetime=datetime_value,
                    open=_coerce_float(item.get("open")),
                    high=_coerce_float(item.get("high")),
                    low=_coerce_float(item.get("low")),
                    close=_coerce_float(item.get("close")),
                    volume=_coerce_int(item.get("volume")),
                )
            )
        return TwelveDataTimeSeries(
            symbol=str(meta_mapping.get("symbol", "")).strip() or symbol,
            name=_clean_optional_text(meta_mapping.get("name")),
            exchange=_clean_optional_text(meta_mapping.get("exchange")),
            instrument_type=_clean_optional_text(meta_mapping.get("type")),
            currency=_clean_optional_text(meta_mapping.get("currency")),
            interval=_clean_optional_text(meta_mapping.get("interval")) or interval,
            values=values,
        )


def _normalize_symbol(symbol: str) -> str:
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise TwelveDataDataError("Symbol cannot be empty.")
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


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _format_http_error_detail(detail: str, *, status_code: int | None = None) -> str:
    trimmed = detail.strip()
    fallback = (
        f"Twelve Data request failed with HTTP {status_code}."
        if status_code is not None
        else "Twelve Data request failed."
    )
    if not trimmed:
        return fallback
    try:
        payload = json.loads(trimmed)
    except json.JSONDecodeError:
        return trimmed
    if not isinstance(payload, Mapping):
        return trimmed
    message = _summarize_error_payload(payload)
    return message or fallback


def _summarize_error_payload(payload: Mapping[str, object]) -> str | None:
    title = _clean_optional_text(payload.get("title"))
    detail = _clean_optional_text(payload.get("detail"))
    error_code = _clean_optional_text(payload.get("error_code"))
    message = _clean_optional_text(payload.get("message"))
    if title and detail:
        if error_code and error_code not in title:
            return f"{title} ({error_code}): {detail}"
        return f"{title}: {detail}"
    if message:
        return message
    return title or detail
