from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from nycti.twelvedata.formatting import (
    format_market_quote_message,
    format_price_history_message,
    format_symbol_suggestions_message,
)
from nycti.twelvedata.models import (
    TwelveDataAPIKeyMissingError,
    TwelveDataDataError,
    TwelveDataHTTPError,
)
from nycti.yahoo import (
    YahooFinanceDataError,
    YahooFinanceHTTPError,
    YahooFinanceNoExtendedHoursError,
    format_yahoo_extended_hours_message,
)


class MarketToolMixin:
    async def _execute_stock_quote_tool(
        self,
        *,
        symbols: list[str],
    ) -> str:
        results = await asyncio.gather(
            *(self._execute_single_stock_quote_tool(symbol=symbol) for symbol in symbols)
        )
        return "\n\n".join(results)

    async def _execute_single_stock_quote_tool(
        self,
        *,
        symbol: str,
    ) -> str:
        try:
            quote = await self.market_data_client.get_market_quote(symbol)
        except TwelveDataAPIKeyMissingError:
            return "Market quote failed because TWELVE_DATA_API_KEY is not configured."
        except TwelveDataHTTPError as exc:
            matches: list[object] = []
            if self._should_search_symbol_matches(str(exc)):
                try:
                    matches = await self.market_data_client.search_symbols(symbol)
                except (TwelveDataAPIKeyMissingError, TwelveDataHTTPError, TwelveDataDataError):
                    matches = []
            if matches:
                return format_symbol_suggestions_message(symbol.upper(), matches)
            detail = str(exc).strip()
            if detail:
                return f"Market quote for `{symbol.upper()}` failed: {detail}"
            return f"Market quote for `{symbol.upper()}` failed because the Twelve Data request failed."
        except TwelveDataDataError:
            return f"Market quote for `{symbol.upper()}` failed because the Twelve Data response was malformed."
        message = format_market_quote_message(quote)
        if _should_try_extended_hours(quote.is_market_open):
            yahoo_message = await self._execute_yahoo_extended_hours_quote_tool(
                symbol=symbol,
                regular_close=quote.close,
            )
            if yahoo_message:
                message += "\n\n" + yahoo_message
        return message

    async def _execute_yahoo_extended_hours_quote_tool(
        self,
        *,
        symbol: str,
        regular_close: float | None,
    ) -> str | None:
        if self.yahoo_finance_client is None:
            return "Yahoo Finance extended-hours fallback unavailable because the client is not configured."
        try:
            quote = await self.yahoo_finance_client.get_extended_hours_quote(symbol)
        except YahooFinanceNoExtendedHoursError:
            return None
        except YahooFinanceHTTPError as exc:
            detail = str(exc).strip()
            if detail:
                return f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed: {detail}"
            return f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed."
        except YahooFinanceDataError:
            return f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed because the response was malformed."
        return format_yahoo_extended_hours_message(quote, regular_close=regular_close)

    async def _execute_price_history_tool(
        self,
        *,
        symbol: str,
        interval: str,
        outputsize: int,
        start_date: str | None,
        end_date: str | None,
    ) -> str:
        try:
            series = await self.market_data_client.get_price_history(
                symbol,
                interval=interval,
                outputsize=outputsize,
                start_date=start_date,
                end_date=end_date,
            )
        except TwelveDataAPIKeyMissingError:
            return "Price history failed because TWELVE_DATA_API_KEY is not configured."
        except TwelveDataHTTPError as exc:
            matches: list[object] = []
            if self._should_search_symbol_matches(str(exc)):
                try:
                    matches = await self.market_data_client.search_symbols(symbol)
                except (TwelveDataAPIKeyMissingError, TwelveDataHTTPError, TwelveDataDataError):
                    matches = []
            if matches:
                return format_symbol_suggestions_message(symbol.upper(), matches)
            detail = str(exc).strip()
            if detail:
                return f"Price history for `{symbol.upper()}` failed: {detail}"
            return f"Price history for `{symbol.upper()}` failed because the Twelve Data request failed."
        except TwelveDataDataError:
            return f"Price history for `{symbol.upper()}` failed because the Twelve Data response was malformed."
        return format_price_history_message(series)

    @staticmethod
    def _should_search_symbol_matches(error_text: str) -> bool:
        normalized = error_text.strip().casefold()
        if not normalized:
            return False
        if any(
            marker in normalized
            for marker in (
                "api key",
                "unauthorized",
                "forbidden",
                "permission",
                "quota",
                "rate limit",
                "too many requests",
                "limit exceeded",
                "temporarily unavailable",
                "internal error",
                "service unavailable",
                "timeout",
                "timed out",
                "connection",
                "network",
            )
        ):
            return False
        return any(
            marker in normalized
            for marker in (
                "symbol",
                "instrument",
                "ticker",
                "not found",
                "unknown",
                "no data",
                "not available",
                "invalid",
            )
        )

    @staticmethod
    def _stock_quote_status(result: str, *, expected_count: int) -> str:
        result_blocks = [block.strip() for block in result.split("\n\n") if block.strip()]
        success_count = sum(block.startswith("Twelve Data market quote for:") for block in result_blocks)
        if success_count == expected_count:
            return "ok"
        if success_count > 0:
            return "mixed"
        if "could not quote" in result:
            return "symbol_suggestions"
        if "not configured" in result:
            return "missing_key"
        if "response was malformed" in result:
            return "data_error"
        if "request failed" in result:
            return "http_error"
        return "unknown"

    @staticmethod
    def _stock_quote_error(result: str) -> str:
        result_blocks = [block.strip() for block in result.split("\n\n") if block.strip()]
        if result_blocks and all(block.startswith("Twelve Data market quote for:") for block in result_blocks):
            return ""
        first_error_block = next(
            (block for block in result_blocks if not block.startswith("Twelve Data market quote for:")),
            result,
        )
        first_line = first_error_block.splitlines()[0].strip()
        return first_line[:240]

    @staticmethod
    def _single_market_result_status(result: str, *, success_prefix: str) -> str:
        normalized = result.strip()
        if normalized.startswith(success_prefix):
            return "ok"
        if "could not quote" in normalized:
            return "symbol_suggestions"
        if "not configured" in normalized:
            return "missing_key"
        if "response was malformed" in normalized:
            return "data_error"
        if "failed:" in normalized or "request failed" in normalized:
            return "http_error"
        return "unknown"

    @staticmethod
    def _single_market_result_error(result: str, *, success_prefix: str) -> str:
        normalized = result.strip()
        if normalized.startswith(success_prefix):
            return ""
        first_line = normalized.splitlines()[0].strip()
        return first_line[:240]


def _should_try_extended_hours(is_market_open: bool | None) -> bool:
    if is_market_open is True:
        return False
    if is_market_open is False:
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return True
    regular_open = time(9, 30)
    regular_close = time(16, 0)
    current_time = now.time()
    return not (regular_open <= current_time < regular_close)
