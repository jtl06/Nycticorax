from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from nycti.browser import (
    BrowserClient,
    BrowserToolDataError,
    BrowserToolDisabledError,
    BrowserToolRuntimeError,
    BrowserToolUnavailableError,
    format_browser_extract_message,
)
from nycti.chat.tools.parsing import (
    parse_browser_extract_arguments,
    parse_channel_context_arguments,
    parse_create_reminder_arguments,
    parse_extract_url_arguments,
    parse_profile_update_arguments,
    parse_python_exec_arguments,
    parse_price_history_arguments,
    parse_send_channel_message_arguments,
    parse_tool_query_argument,
    parse_tool_symbol_list_arguments,
)
from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from nycti.formatting import format_discord_message_link
from nycti.message_context import (
    DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    fetch_older_context_lines,
    format_message_line,
    message_has_visible_content,
)
from nycti.memory.profile import should_attempt_profile_update
from nycti.python_sandbox import PythonSandboxError, run_python_sandbox
from nycti.tavily.formatting import (
    format_tavily_extract_message,
    format_tavily_image_search_message,
    format_tavily_search_message,
)
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
from nycti.timezones import get_timezone
from nycti.twelvedata.client import TwelveDataClient
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

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    import discord
    from sqlalchemy.ext.asyncio import AsyncSession

    from nycti.channel_aliases import ChannelAliasService
    from nycti.db.session import Database
    from nycti.llm.client import OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient


class ChatToolExecutor:
    def __init__(
        self,
        *,
        database: Database,
        settings: object,
        llm_client: OpenAIClient,
        market_data_client: TwelveDataClient,
        tavily_client: TavilyClient,
        browser_client: BrowserClient | None = None,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
        bot: discord.Client,
    ) -> None:
        self.database = database
        self.settings = settings
        self.llm_client = llm_client
        self.market_data_client = market_data_client
        self.tavily_client = tavily_client
        self.browser_client = browser_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self.bot = bot

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, dict[str, int | str]]:
        started_at = time.perf_counter()

        async def finalize(result: str, metrics: dict[str, int | str]) -> tuple[str, dict[str, int | str]]:
            await self._record_tool_call_event(
                tool_name=tool_name,
                result=result,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                latency_ms=_elapsed_ms(started_at),
            )
            return result, metrics

        if tool_name == WEB_SEARCH_TOOL_NAME:
            query = parse_tool_query_argument(arguments)
            if not query:
                return await finalize("Tool call failed because the query argument was missing or invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_web_search_tool(query=query)
            return await finalize(result, {
                "web_search_ms": _elapsed_ms(started_at),
                "web_search_query_count": 1,
            })

        if tool_name == STOCK_QUOTE_TOOL_NAME:
            symbols = parse_tool_symbol_list_arguments(arguments, max_items=5)
            if not symbols:
                return await finalize(
                    "Market quote failed because the `symbol` or `symbols` argument was missing or invalid.",
                    {},
                )
            started_at = time.perf_counter()
            result = await self._execute_stock_quote_tool(symbols=symbols)
            return await finalize(result, {
                "stock_quote_ms": _elapsed_ms(started_at),
                "stock_quote_count": 1,
                "stock_quote_symbol_count": len(symbols),
                "market_data_provider": "twelvedata",
                "stock_quote_symbols": ", ".join(symbols),
                "stock_quote_status": self._stock_quote_status(result, expected_count=len(symbols)),
                "stock_quote_error": self._stock_quote_error(result),
            })

        if tool_name == PRICE_HISTORY_TOOL_NAME:
            payload = parse_price_history_arguments(arguments)
            if payload is None:
                return await finalize((
                    "Price history failed because the `symbol` argument was missing or invalid, "
                    "or one of the optional interval/outputsize values was invalid."
                ), {})
            started_at = time.perf_counter()
            result = await self._execute_price_history_tool(
                symbol=payload.symbol,
                interval=payload.interval,
                outputsize=payload.outputsize,
                start_date=payload.start_date,
                end_date=payload.end_date,
            )
            return await finalize(result, {
                "price_history_ms": _elapsed_ms(started_at),
                "price_history_count": 1,
                "market_data_provider": "twelvedata",
                "price_history_symbol": payload.symbol,
                "price_history_interval": payload.interval,
                "price_history_status": self._single_market_result_status(
                    result,
                    success_prefix="Twelve Data price history for:",
                ),
                "price_history_error": self._single_market_result_error(
                    result,
                    success_prefix="Twelve Data price history for:",
                ),
            })

        if tool_name == GET_CHANNEL_CONTEXT_TOOL_NAME:
            payload = parse_channel_context_arguments(arguments)
            if payload is None:
                return await finalize("Channel context fetch failed because `mode`, `multiplier`, or `expand` was invalid.", {})
            started_at = time.perf_counter()
            result, summary_tokens = await self._execute_get_channel_context_tool(
                mode=payload.mode,
                multiplier=payload.multiplier,
                expand=payload.expand,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
            )
            metrics: dict[str, int | str] = {
                "channel_context_fetch_ms": _elapsed_ms(started_at),
                "channel_context_fetch_count": 1,
                "channel_context_mode": payload.mode,
                "channel_context_multiplier": payload.multiplier,
                "channel_context_expand": "yes" if payload.expand else "no",
                "channel_context_status": (
                    "ok"
                    if result.startswith("Older Discord channel context")
                    else "unavailable"
                ),
            }
            if summary_tokens:
                metrics["channel_context_summary_tokens"] = summary_tokens
            return await finalize(result, metrics)

        if tool_name == IMAGE_SEARCH_TOOL_NAME:
            query = parse_tool_query_argument(arguments)
            if not query:
                return await finalize("Image search failed because the query argument was missing or invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_image_search_tool(query=query)
            return await finalize(result, {
                "image_search_ms": _elapsed_ms(started_at),
                "image_search_query_count": 1,
            })

        if tool_name == EXTRACT_URL_TOOL_NAME:
            payload = parse_extract_url_arguments(arguments)
            if payload is None:
                return await finalize("URL extraction failed because the `url` argument was missing or invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_extract_url_tool(url=payload.url, query=payload.query)
            return await finalize(result, {
                "url_extract_ms": _elapsed_ms(started_at),
                "url_extract_count": 1,
                "url_extract_provider": (
                    "browser"
                    if result.startswith("Browser extract for:")
                    else "tavily"
                ),
            })

        if tool_name == BROWSER_EXTRACT_TOOL_NAME:
            payload = parse_browser_extract_arguments(arguments)
            if payload is None:
                return await finalize("Browser extract failed because `url`, `query`, or `headed` was invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_browser_extract_tool(
                url=payload.url,
                query=payload.query,
                headed=payload.headed,
            )
            return await finalize(result, {
                "browser_extract_ms": _elapsed_ms(started_at),
                "browser_extract_count": 1,
                "browser_extract_headed": "yes" if payload.headed else "no",
            })

        if tool_name == UPDATE_PERSONAL_PROFILE_TOOL_NAME:
            payload = parse_profile_update_arguments(arguments)
            if payload is None:
                return await finalize("Profile update failed because tool arguments were invalid JSON.", {})
            started_at = time.perf_counter()
            result = await self._execute_update_personal_profile_tool(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                note=payload.note,
            )
            return await finalize(result, {
                "profile_update_ms": _elapsed_ms(started_at),
                "profile_update_count": 1,
                "profile_update_status": self._profile_update_status(result),
            })

        if tool_name == PYTHON_EXEC_TOOL_NAME:
            code = parse_python_exec_arguments(arguments)
            if code is None:
                return await finalize("Python execution failed because `code` was missing or invalid.", {})
            started_at = time.perf_counter()
            result = self._execute_python_tool(code=code)
            return await finalize(result, {
                "python_exec_ms": _elapsed_ms(started_at),
                "python_exec_count": 1,
                "python_exec_status": "ok" if result.startswith("Python result") else "error",
            })

        if tool_name == CREATE_REMINDER_TOOL_NAME:
            payload = parse_create_reminder_arguments(arguments)
            if payload is None:
                return await finalize("Reminder creation failed because `message` or `remind_at` was missing or invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_create_reminder_tool(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                reminder_text=payload.message,
                remind_at_text=payload.remind_at,
            )
            return await finalize(result, {
                "reminder_create_ms": _elapsed_ms(started_at),
                "reminder_create_count": 1,
            })

        if tool_name == SEND_CHANNEL_MESSAGE_TOOL_NAME:
            payload = parse_send_channel_message_arguments(arguments)
            if payload is None:
                return await finalize("Channel send failed because `channel` or `message` was missing or invalid.", {})
            started_at = time.perf_counter()
            result = await self._execute_send_channel_message_tool(
                guild_id=guild_id,
                channel_target=payload.channel,
                message_text=payload.message,
            )
            return await finalize(result, {
                "channel_send_ms": _elapsed_ms(started_at),
                "channel_send_count": 1,
            })

        return await finalize(f"Unknown tool `{tool_name}`.", {})

    async def _record_tool_call_event(
        self,
        *,
        tool_name: str,
        result: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        latency_ms: int,
    ) -> None:
        if not hasattr(self.database, "session"):
            return
        status = self._tool_call_status(result)
        try:
            async with self.database.session() as session:
                from nycti.usage import record_tool_call

                await record_tool_call(
                    session,
                    tool_name=tool_name,
                    status=status,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    latency_ms=latency_ms,
                )
                await session.commit()
        except Exception:  # pragma: no cover - defensive telemetry path
            LOGGER.exception("Tool call event logging failed for tool %s.", tool_name)

    @staticmethod
    def _tool_call_status(result: str) -> str:
        normalized = result.strip().casefold()
        if not normalized:
            return "ok"
        if "no older messages beyond the default recent window" in normalized:
            return "empty"
        failure_markers = (
            " failed",
            "unknown tool",
            "not configured",
            "malformed",
            "missing",
            "invalid",
            "unavailable",
            "could not",
        )
        if any(marker in normalized for marker in failure_markers):
            return "error"
        return "ok"

    def _execute_python_tool(self, *, code: str) -> str:
        if not getattr(self.settings, "python_tool_enabled", False):
            return "Python execution failed because PYTHON_TOOL_ENABLED is false."
        try:
            result = run_python_sandbox(
                code,
                timeout_seconds=getattr(self.settings, "python_tool_timeout_seconds", 3.0),
                max_output_chars=getattr(self.settings, "python_tool_max_output_chars", 4000),
            )
        except (PythonSandboxError, SyntaxError, ValueError) as exc:
            return f"Python execution failed: {exc}"
        truncation = "\n(output truncated)" if result.truncated else ""
        return f"Python result ({result.elapsed_ms} ms):\n```text\n{result.output}{truncation}\n```"

    async def _execute_web_search_tool(
        self,
        *,
        query: str,
    ) -> str:
        try:
            search_response = await self.tavily_client.search(query=query, max_results=5)
        except TavilyAPIKeyMissingError:
            return "Web search failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            return f"Web search for `{query}` failed because the Tavily request failed."
        except TavilyDataError:
            return f"Web search for `{query}` failed because the Tavily response was malformed."
        return format_tavily_search_message(search_response, max_items=3)

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
        return format_market_quote_message(quote)

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

    async def _execute_get_channel_context_tool(
        self,
        *,
        mode: str,
        multiplier: int,
        expand: bool,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, int]:
        if channel_id is None or source_message_id is None:
            return "Channel context fetch failed because this request's source channel/message could not be resolved.", 0
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return "Channel context fetch failed because the channel could not be fetched.", 0
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None or not hasattr(channel, "history"):
            return "Channel context fetch failed because this channel does not expose message history.", 0
        try:
            source_message = await fetch_message(source_message_id)
        except Exception:
            return "Channel context fetch failed because the source message could not be fetched.", 0
        base_multiplier = 5 if mode == "raw" else 25
        message_limit = self.settings.channel_context_limit * base_multiplier * multiplier
        line_cap = EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT if expand else DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT
        lines = await fetch_older_context_lines(
            channel,
            before=source_message,
            recent_limit=self.settings.channel_context_limit,
            limit=message_limit,
            content_char_limit=line_cap,
        )
        if not lines:
            return "Channel context fetch found no older messages beyond the default recent window.", 0
        if mode == "raw":
            return (
                "Older Discord channel context (raw, oldest to newest). "
                f"Per-line text cap: {line_cap} chars. Do not paste this block verbatim; "
                "synthesize only what is relevant unless the user explicitly requested raw logs:\n"
                + "\n".join(lines)
            ), 0
        result = await self.llm_client.complete_chat(
            model=self.settings.openai_memory_model,
            feature="extended_context_summary",
            max_tokens=500,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize older Discord channel context for another assistant. "
                        "Keep durable facts, decisions, unresolved questions, and useful references. "
                        "Ignore low-value chatter. Do not invent details. Do not produce a transcript."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Older channel messages, oldest to newest:\n"
                        + "\n".join(lines)
                        + "\n\nReturn a concise bullet summary under 180 words."
                    ),
                },
            ],
        )
        async with self.database.session() as session:
            from nycti.usage import record_usage

            await record_usage(
                session,
                usage=result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            await session.commit()
        return (
            "Older Discord channel context (summary):\n"
            + (result.text.strip() or "(summary was empty)")
        ), result.usage.total_tokens

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

    @staticmethod
    def _profile_update_status(result: str) -> str:
        normalized = result.strip().casefold()
        if "updated" in normalized:
            return "updated"
        if "no durable update" in normalized:
            return "no_change"
        if "skipped" in normalized:
            return "skipped"
        if "failed" in normalized:
            return "error"
        return "ok"

    async def _execute_image_search_tool(
        self,
        *,
        query: str,
    ) -> str:
        try:
            search_response = await self.tavily_client.image_search(query=query, max_results=5)
        except TavilyAPIKeyMissingError:
            return "Image search failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            return f"Image search for `{query}` failed because the Tavily request failed."
        except TavilyDataError:
            return f"Image search for `{query}` failed because the Tavily response was malformed."
        return format_tavily_image_search_message(search_response, max_items=3)

    async def _execute_extract_url_tool(
        self,
        *,
        url: str,
        query: str | None,
    ) -> str:
        try:
            extract_response = await self.tavily_client.extract(url=url, query=query)
        except TavilyAPIKeyMissingError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return "URL extraction failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return f"URL extraction for `{url}` failed because the Tavily request failed."
        except TavilyDataError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return f"URL extraction for `{url}` failed because the Tavily response was malformed."
        return format_tavily_extract_message(extract_response)

    async def _execute_browser_extract_tool(
        self,
        *,
        url: str,
        query: str | None,
        headed: bool,
    ) -> str:
        if self.browser_client is None:
            return "Browser extract failed because browser tooling is not configured."
        try:
            result = await self.browser_client.extract(url=url, query=query, headed=headed)
        except BrowserToolDisabledError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolUnavailableError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolDataError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolRuntimeError as exc:
            return f"Browser extract failed: {exc}"
        return format_browser_extract_message(result)

    async def _try_browser_extract_fallback(
        self,
        *,
        url: str,
        query: str | None,
    ) -> str | None:
        if self.browser_client is None:
            return None
        try:
            result = await self.browser_client.extract(url=url, query=query, headed=False)
        except (BrowserToolDisabledError, BrowserToolUnavailableError, BrowserToolDataError, BrowserToolRuntimeError):
            return None
        return format_browser_extract_message(result)

    async def _execute_update_personal_profile_tool(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        note: str | None,
    ) -> str:
        current_message = (note or "").strip()
        source_message_text, source_context_lines = await self._resolve_source_message_context(
            channel_id=channel_id,
            source_message_id=source_message_id,
        )
        if not current_message:
            current_message = source_message_text
        if not current_message:
            return "Profile update skipped because there was no current message text to evaluate."
        if not should_attempt_profile_update(current_message):
            return (
                "Profile update skipped because the message referenced another user "
                "without caller-specific personal signal."
            )

        recent_context = "\n".join(source_context_lines) or "(none)"
        async with self.database.session() as session:
            profile_before = await self.memory_service.get_personal_profile_md(session, user_id)
            result = await self.memory_service.maybe_update_personal_profile(
                session,
                user_id=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                current_message=current_message,
                recent_context=recent_context,
            )
            if result is None:
                return "Profile update skipped because memory is disabled for this user."
            from nycti.usage import record_usage

            await record_usage(
                session,
                usage=result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            profile_after = await self.memory_service.get_personal_profile_md(session, user_id)
            await session.commit()

        if profile_after != profile_before:
            return "Profile note updated."
        return "Profile note checked; no durable update was needed."

    async def _resolve_source_message_context(
        self,
        *,
        channel_id: int | None,
        source_message_id: int | None,
    ) -> tuple[str, list[str]]:
        if channel_id is None or source_message_id is None:
            return "", []
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return "", []
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return "", []
        try:
            source_message = await fetch_message(source_message_id)
        except Exception:
            return "", []

        source_text = " ".join(str(getattr(source_message, "content", "") or "").split()).strip()
        attachments = getattr(source_message, "attachments", [])
        if not source_text and attachments:
            source_text = f"[{len(attachments)} attachment(s)]"

        if not hasattr(channel, "history"):
            return source_text, []
        history_messages: list[object] = []
        try:
            async for item in channel.history(
                limit=self.settings.channel_context_limit,
                before=source_message,
                oldest_first=False,
            ):
                history_messages.append(item)
        except Exception:
            return source_text, []

        history_messages.reverse()
        context_lines = [
            format_message_line(item, include_timestamp=True)
            for item in history_messages
            if message_has_visible_content(item)
        ]
        return source_text, context_lines

    async def _execute_create_reminder_tool(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        reminder_text: str,
        remind_at_text: str,
    ) -> str:
        if channel_id is None:
            return "Reminder creation failed because this channel could not be resolved."
        async with self.database.session() as session:
            timezone_name = await self.memory_service.get_timezone_name(session, user_id)
            user_timezone = get_timezone(timezone_name)
            parsed = self.reminder_service.parse_remind_at(
                remind_at_text,
                now=datetime.now(timezone.utc).astimezone(user_timezone),
            )
            if parsed is None:
                return (
                    "Reminder creation failed because `remind_at` was invalid. "
                    "Use an ISO 8601 local date or date-time, like `2026-03-22` or `2026-03-22T15:30:00-07:00`."
                )
            remind_at_utc = parsed.remind_at.astimezone(timezone.utc)
            if remind_at_utc <= datetime.now(timezone.utc):
                return "Reminder creation failed because the requested time is not in the future."
            reminder = await self.reminder_service.create_reminder(
                session,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                reminder_text=reminder_text,
                remind_at=remind_at_utc,
            )
            local_remind_at = parsed.remind_at.astimezone(user_timezone)
            await session.commit()
        reminder_line = (
            f"Reminder `{reminder.id}` created for {local_remind_at.strftime('%Y-%m-%d %H:%M:%S %Z')}: "
            f"{reminder.reminder_text}"
        )
        if parsed.assumed_time:
            reminder_line += " (assumed 09:00 local time because only a date was provided)"
        if source_message_id is not None:
            jump_link = format_discord_message_link(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=source_message_id,
            )
            reminder_line += f"\nOriginal message: {jump_link}"
        return reminder_line

    async def _execute_send_channel_message_tool(
        self,
        *,
        guild_id: int | None,
        channel_target: str,
        message_text: str,
    ) -> str:
        if guild_id is None:
            return "Channel send failed because this request was not tied to a server."
        cleaned_target = channel_target.strip()
        if cleaned_target.isdigit():
            resolved_channel_id = int(cleaned_target)
        else:
            async with self.database.session() as session:
                resolved_channel_id = await self.channel_alias_service.resolve_channel_id(
                    session,
                    guild_id=guild_id,
                    channel=channel_target,
                )
        if resolved_channel_id is None:
            return "Channel send failed because that alias or channel ID is unknown in this server."
        channel = self.bot.get_channel(resolved_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(resolved_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return f"Channel send failed because channel `{channel_target}` could not be fetched."
        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or channel_guild.id != guild_id:
            return "Channel send failed because the target channel is not in this server."
        try:
            await channel.send(message_text)
        except (discord.Forbidden, discord.HTTPException):
            return f"Channel send failed because the bot could not send to `{channel_target}`."
        return f"Sent message to <#{resolved_channel_id}>."


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
