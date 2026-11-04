from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from nycti.twelvedata.formatting import (
    format_market_quote_message,
    format_symbol_suggestions_message,
)
from nycti.twelvedata.models import (
    TwelveDataAPIKeyMissingError,
    TwelveDataDataError,
    TwelveDataHTTPError,
)
from nycti.chat.tools.history import execute_history
from nycti.yahoo import (
    YahooFinanceDataError,
    YahooFinanceHTTPError,
    YahooFinanceNoExtendedHoursError,
    YahooMarketSnapshot,
    format_annual_performance,
    format_yahoo_current_session_quote_message,
    format_yahoo_extended_hours_message,
    format_yahoo_market_snapshot_message,
    yahoo_extended_hours_from_snapshot,
)

MARKET_QUOTE_SUCCESS_PREFIXES = (
    "Twelve Data market quote for:",
    "Yahoo Finance current-session fallback for:",
    "Yahoo Finance extended-hours fallback for:",
)
YAHOO_PRIMARY_FALLBACK_MARKER = "Primary quote provider was unavailable"


class MarketToolMixin:
    async def _execute_annual_performance_tool(
        self,
        *,
        symbols: list[str],
        start_year: int,
    ) -> str:
        if self.yahoo_finance_client is None:
            return "Annual performance failed because the Yahoo Finance client is not configured."

        async def fetch_one(symbol: str) -> str:
            try:
                performance = await self.yahoo_finance_client.get_annual_performance(
                    symbol,
                    start_year=start_year,
                )
            except YahooFinanceHTTPError as exc:
                return f"Annual performance for `{symbol}` failed: {str(exc).strip() or 'Yahoo request failed.'}"
            except YahooFinanceDataError:
                return f"Annual performance for `{symbol}` failed because Yahoo returned malformed history."
            return format_annual_performance(performance)

        return "\n\n".join(await asyncio.gather(*(fetch_one(symbol) for symbol in symbols)))

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
        snapshot_prefetched = self.yahoo_finance_client is not None
        primary_result, snapshot_result = await asyncio.gather(
            self.market_data_client.get_market_quote(symbol),
            (
                self._get_yahoo_market_snapshot(symbol)
                if snapshot_prefetched
                else asyncio.sleep(0, result=None)
            ),
            return_exceptions=True,
        )
        if isinstance(primary_result, TwelveDataHTTPError):
            matches: list[object] = []
            if self._should_search_symbol_matches(str(primary_result)):
                try:
                    matches = await self.market_data_client.search_symbols(symbol)
                except (TwelveDataAPIKeyMissingError, TwelveDataHTTPError, TwelveDataDataError):
                    matches = []
            if matches:
                return format_symbol_suggestions_message(symbol.upper(), matches)
        if isinstance(snapshot_result, BaseException):
            raise snapshot_result
        yahoo_snapshot = (
            snapshot_result
            if isinstance(snapshot_result, YahooMarketSnapshot)
            else None
        )
        if isinstance(primary_result, TwelveDataAPIKeyMissingError):
            yahoo_fallback = await self._yahoo_quote_after_primary_failure(
                symbol,
                prefetched_snapshot=yahoo_snapshot,
                snapshot_prefetched=snapshot_prefetched,
            )
            if yahoo_fallback:
                return yahoo_fallback
            return "Market quote failed because TWELVE_DATA_API_KEY is not configured."
        if isinstance(primary_result, TwelveDataHTTPError):
            yahoo_fallback = await self._yahoo_quote_after_primary_failure(
                symbol,
                prefetched_snapshot=yahoo_snapshot,
                snapshot_prefetched=snapshot_prefetched,
            )
            if yahoo_fallback:
                return yahoo_fallback
            detail = str(primary_result).strip()
            if detail:
                return f"Market quote for `{symbol.upper()}` failed: {detail}"
            return f"Market quote for `{symbol.upper()}` failed because the Twelve Data request failed."
        if isinstance(primary_result, TwelveDataDataError):
            yahoo_fallback = await self._yahoo_quote_after_primary_failure(
                symbol,
                prefetched_snapshot=yahoo_snapshot,
                snapshot_prefetched=snapshot_prefetched,
            )
            if yahoo_fallback:
                return yahoo_fallback
            return f"Market quote for `{symbol.upper()}` failed because the Twelve Data response was malformed."
        if isinstance(primary_result, BaseException):
            raise primary_result
        quote = primary_result
        message = format_market_quote_message(quote)
        if _should_try_extended_hours(quote.is_market_open):
            snapshot_extended_quote = (
                yahoo_extended_hours_from_snapshot(yahoo_snapshot)
                if yahoo_snapshot is not None
                else None
            )
            if snapshot_extended_quote is not None:
                yahoo_message = format_yahoo_extended_hours_message(
                    snapshot_extended_quote,
                    regular_close=quote.close,
                )
                yahoo_regular_close = snapshot_extended_quote.regular_price
            else:
                (
                    yahoo_message,
                    yahoo_regular_close,
                ) = await self._execute_yahoo_extended_hours_quote_tool(
                    symbol=symbol,
                    regular_close=quote.close,
                )
            if yahoo_message:
                if (
                    yahoo_regular_close is not None
                    and quote.close is not None
                    and abs(yahoo_regular_close - quote.close) >= 0.01
                ):
                    message = format_market_quote_message(
                        quote,
                        include_price_details=False,
                    )
                    message += (
                        "\nProvider reconciliation: Yahoo's same-page regular and extended-hours "
                        "prices override the conflicting Twelve Data price fields."
                    )
                # Lead with the later session, not the regular close repeated in context.
                if yahoo_message.startswith("Yahoo Finance extended-hours fallback for:"):
                    message = yahoo_message + "\n\n" + message
                else:
                    message += "\n\n" + yahoo_message
        if yahoo_snapshot is not None:
            valuation_message = format_yahoo_market_snapshot_message(yahoo_snapshot)
            if valuation_message:
                message += "\n\n" + valuation_message
        return message

    async def _get_yahoo_market_snapshot(
        self,
        symbol: str,
    ) -> YahooMarketSnapshot | None:
        if self.yahoo_finance_client is None:
            return None
        getter = getattr(self.yahoo_finance_client, "get_market_snapshot", None)
        if not callable(getter):
            return None
        try:
            return await getter(symbol)
        except (YahooFinanceHTTPError, YahooFinanceDataError):
            return None

    async def _yahoo_quote_after_primary_failure(
        self,
        symbol: str,
        *,
        prefetched_snapshot: YahooMarketSnapshot | None = None,
        snapshot_prefetched: bool = False,
    ) -> str | None:
        snapshot = (
            prefetched_snapshot
            if snapshot_prefetched
            else await self._get_yahoo_market_snapshot(symbol)
        )
        if snapshot is not None:
            extended_quote = yahoo_extended_hours_from_snapshot(snapshot)
            if extended_quote is not None:
                message = format_yahoo_extended_hours_message(
                    extended_quote,
                    regular_close=snapshot.regular_price,
                )
            else:
                message = format_yahoo_current_session_quote_message(snapshot)
            if message:
                message += f"\n{YAHOO_PRIMARY_FALLBACK_MARKER}; using Yahoo's current session data."
                valuation = format_yahoo_market_snapshot_message(snapshot)
                return message + "\n\n" + valuation if valuation else message
        message, _regular_close = await self._execute_yahoo_extended_hours_quote_tool(
            symbol=symbol,
            regular_close=None,
        )
        if message and message.startswith("Yahoo Finance extended-hours fallback for:"):
            return message + f"\n{YAHOO_PRIMARY_FALLBACK_MARKER}; using Yahoo's current session data."
        return None

    async def _execute_yahoo_extended_hours_quote_tool(
        self,
        *,
        symbol: str,
        regular_close: float | None,
    ) -> tuple[str | None, float | None]:
        if self.yahoo_finance_client is None:
            return (
                "Yahoo Finance extended-hours fallback unavailable because the client is not configured.",
                None,
            )
        try:
            quote = await self.yahoo_finance_client.get_extended_hours_quote(symbol)
        except YahooFinanceNoExtendedHoursError:
            return None, None
        except YahooFinanceHTTPError as exc:
            detail = str(exc).strip()
            if detail:
                return (
                    f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed: {detail}",
                    None,
                )
            return f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed.", None
        except YahooFinanceDataError:
            return (
                f"Yahoo Finance extended-hours fallback for `{symbol.upper()}` failed because the response was malformed.",
                None,
            )
        return (
            format_yahoo_extended_hours_message(quote, regular_close=regular_close),
            quote.regular_price,
        )

    async def _execute_price_history_tool(
        self,
        *,
        symbol: str,
        mode: str,
        interval: str,
        outputsize: int,
        start_date: str | None,
        end_date: str | None,
    ) -> str:
        return await execute_history(self, symbol=symbol, mode=mode, interval=interval,
            outputsize=outputsize, start_date=start_date, end_date=end_date)

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
    def _stock_quote_success_count(result: str) -> int:
        result_blocks = [block.strip() for block in result.split("\n\n") if block.strip()]
        twelve_data_count = sum(
            block.startswith("Twelve Data market quote for:") for block in result_blocks
        )
        yahoo_only_count = sum(
            block.startswith(
                (
                    "Yahoo Finance current-session fallback for:",
                    "Yahoo Finance extended-hours fallback for:",
                )
            )
            and YAHOO_PRIMARY_FALLBACK_MARKER in block
            for block in result_blocks
        )
        return twelve_data_count + yahoo_only_count

    @staticmethod
    def _stock_quote_provider(result: str) -> str:
        has_twelve_data = "Twelve Data market quote for:" in result
        has_yahoo = (
            "Yahoo Finance current-session fallback for:" in result
            or "Yahoo Finance extended-hours fallback for:" in result
            or "Yahoo Finance public-company valuation for:" in result
        )
        if has_twelve_data and has_yahoo:
            return "twelvedata+yahoo"
        if has_yahoo:
            return "yahoo"
        return "twelvedata"

    @staticmethod
    def _stock_quote_valuation_count(result: str) -> int:
        return result.count("Yahoo Finance public-company valuation for:")

    @staticmethod
    def _stock_quote_status(result: str, *, expected_count: int) -> str:
        success_count = MarketToolMixin._stock_quote_success_count(result)
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
        valid_prefixes = (*MARKET_QUOTE_SUCCESS_PREFIXES, "Yahoo Finance public-company valuation for:")
        if result_blocks and all(block.startswith(valid_prefixes) for block in result_blocks):
            return ""
        first_error_block = next(
            (block for block in result_blocks if not block.startswith(valid_prefixes)),
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
