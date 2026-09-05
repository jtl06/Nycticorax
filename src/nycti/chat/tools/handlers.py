from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tools.parsing import (
    AnnualPerformanceToolArguments,
    BrowserExtractToolArguments,
    ChannelContextToolArguments,
    ChannelMessageToolArguments,
    DeepResearchToolArguments,
    MemorySearchToolArguments,
    PriceHistoryToolArguments,
    ReminderToolArguments,
    UrlExtractToolArguments,
    WebSearchToolArguments,
    YouTubeTranscriptToolArguments,
)
from nycti.timing import elapsed_ms


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    guild_id: int | None
    channel_id: int | None
    user_id: int
    source_message_id: int | None
    permissions: AgentPermissions
    run_id: str
    step_index: int


class RegisteredToolHandlerMixin:
    deep_research_semaphore: asyncio.Semaphore

    async def _handle_deep_research(
        self,
        payload: DeepResearchToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        async with self.deep_research_semaphore:
            return await self._execute_deep_research_tool(
                question=payload.question,
                focus=payload.focus,
            )

    async def _handle_memory_search(
        self,
        payload: MemorySearchToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return await self._execute_memory_search_tool(
            requester_user_id=context.user_id,
            guild_id=context.guild_id,
            query=payload.query,
            owner_user_ids=payload.owner_user_ids,
            visibility_scopes=payload.visibility_scopes,
        )

    async def _handle_web_search(
        self,
        payload: WebSearchToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_web_search_tool(
            queries=list(payload.queries),
            topic=payload.topic,
            time_range=payload.time_range,
            country=payload.country,
        )
        metrics = {
            "web_search_ms": elapsed_ms(started_at),
            "web_search_query_count": len(payload.queries),
        }
        if payload.topic:
            metrics["web_search_topic"] = payload.topic
        if payload.time_range:
            metrics["web_search_time_range"] = payload.time_range
        if payload.country:
            metrics["web_search_country"] = payload.country
        return _result_from_prefixes(
            result,
            metrics,
            success_prefixes=("Tavily web results for:",),
            empty_prefixes=("No web results found for:",),
        )

    async def _handle_stock_quote(
        self,
        symbols: list[str],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_stock_quote_tool(symbols=symbols)
        metrics = {
            "stock_quote_ms": elapsed_ms(started_at),
            "stock_quote_count": 1,
            "stock_quote_symbol_count": len(symbols),
            "stock_quote_success_symbol_count": self._stock_quote_success_count(result),
            "stock_quote_timestamp_count": result.count("Quote time:"),
            "stock_quote_market_state_count": result.count("market state:"),
            "stock_quote_valuation_symbol_count": self._stock_quote_valuation_count(result),
            "market_data_provider": self._stock_quote_provider(result),
            "stock_quote_symbols": ", ".join(symbols),
            "stock_quote_status": self._stock_quote_status(result, expected_count=len(symbols)),
            "stock_quote_error": self._stock_quote_error(result),
        }
        quote_status = str(metrics["stock_quote_status"])
        status = ToolStatus.OK if quote_status in {"ok", "mixed"} else ToolStatus.EMPTY if quote_status == "symbol_suggestions" else ToolStatus.ERROR
        return ToolExecutionResult(content=result, status=status, metrics=metrics)

    async def _handle_price_history(
        self,
        payload: PriceHistoryToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_price_history_tool(
            symbol=payload.symbol,
            mode=payload.mode,
            interval=payload.interval,
            outputsize=payload.outputsize,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
        metrics = {
            "price_history_ms": elapsed_ms(started_at),
            "price_history_count": 1,
            "market_data_provider": "twelvedata",
            "price_history_symbol": payload.symbol,
            "price_history_mode": payload.mode,
            "price_history_interval": payload.interval,
            "price_history_status": self._single_market_result_status(
                result,
                success_prefix="Twelve Data price history for:",
            ),
            "price_history_error": self._single_market_result_error(
                result,
                success_prefix="Twelve Data price history for:",
            ),
        }
        history_status = str(metrics["price_history_status"])
        status = ToolStatus.OK if history_status == "ok" else ToolStatus.EMPTY if history_status == "symbol_suggestions" else ToolStatus.ERROR
        return ToolExecutionResult(content=result, status=status, metrics=metrics)

    async def _handle_annual_performance(
        self,
        payload: AnnualPerformanceToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        start_year = payload.start_year or datetime.now(timezone.utc).year - 6
        started_at = time.perf_counter()
        result = await self._execute_annual_performance_tool(
            symbols=list(payload.symbols),
            start_year=start_year,
        )
        return _result_from_prefixes(result, {
            "annual_performance_ms": elapsed_ms(started_at),
            "annual_performance_count": 1,
            "annual_performance_symbol_count": len(payload.symbols),
            "annual_performance_symbols": ", ".join(payload.symbols),
            "market_data_provider": "yahoo",
        }, success_prefixes=("Yahoo Finance annual performance for ",))

    async def _handle_channel_context(
        self,
        payload: ChannelContextToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result, summary_tokens = await self._execute_get_channel_context_tool(
            mode=payload.mode,
            multiplier=payload.multiplier,
            expand=payload.expand,
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            user_id=context.user_id,
            source_message_id=context.source_message_id,
        )
        metrics: dict[str, int | str] = {
            "channel_context_fetch_ms": elapsed_ms(started_at),
            "channel_context_fetch_count": 1,
            "channel_context_mode": payload.mode,
            "channel_context_multiplier": payload.multiplier,
            "channel_context_expand": "yes" if payload.expand else "no",
            "channel_context_status": (
                "ok" if result.startswith("Older Discord channel context") else "unavailable"
            ),
        }
        if summary_tokens:
            metrics["channel_context_summary_tokens"] = summary_tokens
        return _result_from_prefixes(
            result,
            metrics,
            success_prefixes=("Older Discord channel context",),
            empty_prefixes=("No older messages",),
        )

    async def _handle_image_search(
        self,
        query: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_image_search_tool(query=query)
        return _result_from_prefixes(result, {
            "image_search_ms": elapsed_ms(started_at),
            "image_search_query_count": 1,
        }, success_prefixes=("Tavily image results for:",), empty_prefixes=("No image results found for:",))

    async def _handle_url_extract(
        self,
        payload: UrlExtractToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_extract_url_tool(url=payload.url, query=payload.query)
        return _result_from_prefixes(result, {
            "url_extract_ms": elapsed_ms(started_at),
            "url_extract_count": 1,
            "url_extract_provider": "browser" if result.startswith("Browser extract for:") else "tavily",
        }, success_prefixes=("Tavily extract for:", "Browser extract for:"), empty_prefixes=("No extractable content found for:",))

    async def _handle_browser_extract(
        self,
        payload: BrowserExtractToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_browser_extract_tool(
            url=payload.url,
            query=payload.query,
            headed=payload.headed,
        )
        return _result_from_prefixes(result, {
            "browser_extract_ms": elapsed_ms(started_at),
            "browser_extract_count": 1,
            "browser_extract_headed": "yes" if payload.headed else "no",
        }, success_prefixes=("Browser extract for:",))

    async def _handle_youtube_transcript(
        self,
        payload: YouTubeTranscriptToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result, summary_tokens = await self._execute_youtube_transcript_tool(
            url=payload.url,
            query=payload.query,
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            user_id=context.user_id,
        )
        metrics: dict[str, int | str] = {
            "youtube_transcript_ms": elapsed_ms(started_at),
            "youtube_transcript_count": 1,
            "youtube_transcript_status": (
                "ok" if result.startswith("YouTube transcript summary for:") else "error"
            ),
        }
        if summary_tokens:
            metrics["youtube_transcript_summary_tokens"] = summary_tokens
        return _result_from_prefixes(
            result,
            metrics,
            success_prefixes=("YouTube transcript summary for:",),
        )

    async def _handle_python(
        self,
        code: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._execute_python_tool(code=code)
        return _result_from_prefixes(result, {
            "python_exec_ms": elapsed_ms(started_at),
            "python_exec_count": 1,
            "python_exec_status": "ok" if result.startswith("Python result") else "error",
        }, success_prefixes=("Python result",))

    async def _handle_report_issue(
        self,
        reason: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        if context.guild_id is None or context.channel_id is None or context.source_message_id is None:
            return _error("Response issue logging is available only for a Discord server message.")

        from nycti.feedback import record_response_feedback
        from nycti.formatting import format_discord_message_link

        cache = getattr(self.bot, "_response_diagnostic_cache", None)
        if cache is None:
            return _error("No recent response diagnostics were available to log.")
        started_at = time.perf_counter()
        result = await record_response_feedback(
            self.bot,
            database=self.database,
            debug_channel_id=getattr(self.settings, "error_debug_channel_id", None),
            persist_snapshots=bool(
                getattr(self.settings, "persist_bad_bot_diagnostics", False)
            ),
            cache=cache,
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            feedback_message_id=context.source_message_id,
            feedback_message_url=format_discord_message_link(
                guild_id=context.guild_id,
                channel_id=context.channel_id,
                message_id=context.source_message_id,
            ),
            feedback_user_id=context.user_id,
            feedback_text=f"Nycti self-report: {reason}",
            allow_latest=True,
        )
        if result.logged and context.source_message_id is not None:
            marker = getattr(self, "mark_memory_correction", None)
            if callable(marker):
                marker(context.source_message_id)
        metrics = {
            "response_issue_log_ms": elapsed_ms(started_at),
            "response_issue_log_count": int(result.logged),
        }
        if not result.found:
            return _error(
                "No recent Nycti response diagnostics were available to log. Continue correcting the answer.",
                metrics=metrics,
            )
        if not result.logged:
            return _error(
                "The prior response was found, but its diagnostics could not be archived or sent. Continue correcting the answer.",
                metrics=metrics,
            )
        return ToolExecutionResult(
            content="The previous Nycti response was logged for review. Now correct the answer without dwelling on the log.",
            status=ToolStatus.OK,
            metrics=metrics,
        )

    async def _handle_create_reminder(
        self,
        payload: ReminderToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._propose_create_reminder_tool(
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            user_id=context.user_id,
            source_message_id=context.source_message_id,
            reminder_text=payload.message,
            remind_at_text=payload.remind_at,
        )
        return _result_from_prefixes(result, {
            "action_proposal_ms": elapsed_ms(started_at),
            "action_proposal_attempt_count": 1,
            "action_proposal_count": int(result.startswith("Confirmation required\n")),
            "action_proposal_kind": "create_reminder",
        }, success_prefixes=("Confirmation required",))

    async def _handle_send_message(
        self,
        payload: ChannelMessageToolArguments,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        started_at = time.perf_counter()
        result = await self._propose_send_channel_message_tool(
            guild_id=context.guild_id,
            request_channel_id=context.channel_id,
            user_id=context.user_id,
            source_message_id=context.source_message_id,
            channel_target=payload.channel,
            message_text=payload.message,
        )
        return _result_from_prefixes(result, {
            "action_proposal_ms": elapsed_ms(started_at),
            "action_proposal_attempt_count": 1,
            "action_proposal_count": int(result.startswith("Confirmation required\n")),
            "action_proposal_kind": "send_channel_message",
        }, success_prefixes=("Confirmation required",))


def _error(
    content: str,
    *,
    retryable: bool = False,
    metrics: dict[str, int | str] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        content=content,
        status=ToolStatus.ERROR,
        metrics=metrics or {},
        retryable=retryable,
    )


def _result_from_prefixes(
    content: str,
    metrics: dict[str, int | str],
    *,
    success_prefixes: tuple[str, ...],
    empty_prefixes: tuple[str, ...] = (),
) -> ToolExecutionResult:
    blocks = [block.strip() for block in content.split("\n\n") if block.strip()]
    if any(block.startswith(success_prefixes) for block in blocks):
        status = ToolStatus.OK
    elif any(block.startswith(empty_prefixes) for block in blocks):
        status = ToolStatus.EMPTY
    else:
        status = ToolStatus.ERROR
    return ToolExecutionResult(content=content, status=status, metrics=metrics)
