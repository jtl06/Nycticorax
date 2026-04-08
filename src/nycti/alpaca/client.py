from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from nycti.alpaca.models import (
    AlpacaAPIKeyMissingError,
    AlpacaBar,
    AlpacaDataError,
    AlpacaHTTPError,
    AlpacaQuote,
    AlpacaStockSnapshot,
    AlpacaTrade,
)


ALPACA_MARKET_DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaClient:
    def __init__(
        self,
        api_key_id: str | None,
        api_secret_key: str | None,
        *,
        base_url: str = ALPACA_MARKET_DATA_BASE_URL,
        stock_feed: str = "iex",
        timeout_seconds: float = 10.0,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.api_key_id = api_key_id.strip() if api_key_id and api_key_id.strip() else None
        self.api_secret_key = api_secret_key.strip() if api_secret_key and api_secret_key.strip() else None
        self.base_url = base_url.rstrip("/")
        self.stock_feed = stock_feed.strip().lower() or "iex"
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._fetch_json_sync

    async def get_stock_snapshot(self, symbol: str) -> AlpacaStockSnapshot:
        if self.api_key_id is None or self.api_secret_key is None:
            raise AlpacaAPIKeyMissingError(
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be configured before using stock quotes."
            )
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise AlpacaDataError("Stock symbol cannot be empty.")
        query = urlencode({"feed": self.stock_feed})
        url = f"{self.base_url}/v2/stocks/{normalized_symbol}/snapshot?{query}"
        payload = await asyncio.to_thread(self._fetch_json, url)
        return self._parse_snapshot(normalized_symbol, payload)

    def _fetch_json_sync(self, url: str) -> object:
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "APCA-API-KEY-ID": self.api_key_id or "",
                "APCA-API-SECRET-KEY": self.api_secret_key or "",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = detail or f"Alpaca request failed with HTTP {exc.code}."
            raise AlpacaHTTPError(message) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise AlpacaHTTPError(f"Alpaca request failed: {reason}.") from exc

        if not raw:
            raise AlpacaDataError("Alpaca response was empty.")

        try:
            text = raw.decode(charset)
        except UnicodeDecodeError as exc:
            raise AlpacaDataError("Alpaca response was not valid text.") from exc

        try:
            response_payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AlpacaDataError("Alpaca response was not valid JSON.") from exc

        if not isinstance(response_payload, Mapping):
            raise AlpacaDataError("Alpaca response had an unexpected shape.")
        return response_payload

    def _parse_snapshot(self, symbol: str, payload: object) -> AlpacaStockSnapshot:
        if not isinstance(payload, Mapping):
            raise AlpacaDataError("Alpaca response had an unexpected shape.")
        return AlpacaStockSnapshot(
            symbol=symbol,
            latest_trade=self._parse_trade(payload.get("latestTrade")),
            latest_quote=self._parse_quote(payload.get("latestQuote")),
            daily_bar=self._parse_bar(payload.get("dailyBar")),
            previous_daily_bar=self._parse_bar(payload.get("prevDailyBar")),
            feed=self.stock_feed,
        )

    def _parse_trade(self, payload: object) -> AlpacaTrade | None:
        if not isinstance(payload, Mapping):
            return None
        price = _coerce_float(payload.get("p"))
        timestamp = str(payload.get("t", "")).strip()
        if price is None or not timestamp:
            return None
        return AlpacaTrade(
            price=price,
            timestamp=timestamp,
            size=_coerce_int(payload.get("s")),
            exchange=str(payload.get("x", "")).strip() or None,
        )

    def _parse_quote(self, payload: object) -> AlpacaQuote | None:
        if not isinstance(payload, Mapping):
            return None
        bid_price = _coerce_float(payload.get("bp"))
        ask_price = _coerce_float(payload.get("ap"))
        timestamp = str(payload.get("t", "")).strip()
        if bid_price is None or ask_price is None:
            return None
        return AlpacaQuote(
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=_coerce_int(payload.get("bs")),
            ask_size=_coerce_int(payload.get("as")),
            timestamp=timestamp,
        )

    def _parse_bar(self, payload: object) -> AlpacaBar | None:
        if not isinstance(payload, Mapping):
            return None
        close = _coerce_float(payload.get("c"))
        if close is None:
            return None
        return AlpacaBar(
            close=close,
            open=_coerce_float(payload.get("o")),
            high=_coerce_float(payload.get("h")),
            low=_coerce_float(payload.get("l")),
            volume=_coerce_int(payload.get("v")),
            timestamp=str(payload.get("t", "")).strip(),
        )


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
