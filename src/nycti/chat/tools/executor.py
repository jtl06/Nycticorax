from __future__ import annotations

import time
from typing import TYPE_CHECKING

from nycti.chat.tools.actions import ActionToolMixin
from nycti.chat.tools.content import ContentToolMixin
from nycti.chat.tools.market import MarketToolMixin
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
    parse_youtube_transcript_arguments,
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
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)
from nycti.chat.tools.telemetry import ToolTelemetryMixin, _elapsed_ms

if TYPE_CHECKING:
    import discord

    from nycti.browser import BrowserClient
    from nycti.channel_aliases import ChannelAliasService
    from nycti.db.session import Database
    from nycti.llm.client import OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient
    from nycti.yahoo import YahooFinanceClient
    from nycti.youtube import YouTubeTranscriptClient


class ChatToolExecutor(ActionToolMixin, ContentToolMixin, MarketToolMixin, ToolTelemetryMixin):
    def __init__(
        self,
        *,
        database: Database,
        settings: object,
        llm_client: OpenAIClient,
        market_data_client: TwelveDataClient,
        tavily_client: TavilyClient,
        yahoo_finance_client: YahooFinanceClient | None = None,
        browser_client: BrowserClient | None = None,
        youtube_client: YouTubeTranscriptClient | None = None,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
        bot: discord.Client,
    ) -> None:
        self.database = database
        self.settings = settings
        self.llm_client = llm_client
        self.market_data_client = market_data_client
        self.yahoo_finance_client = yahoo_finance_client
        self.tavily_client = tavily_client
        self.browser_client = browser_client
        self.youtube_client = youtube_client
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
                "market_data_provider": (
                    "twelvedata+yahoo"
                    if "Yahoo Finance extended-hours fallback" in result
                    else "twelvedata"
                ),
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

        if tool_name == YOUTUBE_TRANSCRIPT_TOOL_NAME:
            payload = parse_youtube_transcript_arguments(arguments)
            if payload is None:
                return await finalize("YouTube transcript extraction failed because the `url` argument was missing or invalid.", {})
            started_at = time.perf_counter()
            result, summary_tokens = await self._execute_youtube_transcript_tool(
                url=payload.url,
                query=payload.query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            metrics: dict[str, int | str] = {
                "youtube_transcript_ms": _elapsed_ms(started_at),
                "youtube_transcript_count": 1,
                "youtube_transcript_status": "ok" if result.startswith("YouTube transcript summary for:") else "error",
            }
            if summary_tokens:
                metrics["youtube_transcript_summary_tokens"] = summary_tokens
            return await finalize(result, metrics)

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
