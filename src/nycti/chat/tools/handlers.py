from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import time

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tools.parsing import (
    parse_annual_performance_arguments,
    parse_browser_extract_arguments,
    parse_channel_context_arguments,
    parse_create_reminder_arguments,
    parse_extract_url_arguments,
    parse_python_exec_arguments,
    parse_price_history_arguments,
    parse_send_channel_message_arguments,
    parse_tool_query_argument,
    parse_web_search_arguments,
    parse_tool_symbol_list_arguments,
    parse_youtube_transcript_arguments,
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

    def action_key(self, tool_name: str, arguments: str) -> str:
        source = str(self.source_message_id) if self.source_message_id is not None else self.run_id
        normalized_arguments = arguments.strip()
        try:
            parsed_arguments = json.loads(normalized_arguments) if normalized_arguments else {}
        except json.JSONDecodeError:
            pass
        else:
            normalized_arguments = json.dumps(
                parsed_arguments,
                sort_keys=True,
                separators=(",", ":"),
            )
        payload = f"{source}:{self.user_id}:{tool_name}:{normalized_arguments}".encode()
        return hashlib.sha256(payload).hexdigest()


class RegisteredToolHandlerMixin:
    async def _handle_web_search(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_web_search_arguments(arguments, max_items=4)
        if payload is None:
            return _error("Tool call failed because the query argument was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_web_search_tool(
            queries=list(payload.queries),
            topic=payload.topic,
            time_range=payload.time_range,
        )
        metrics = {
            "web_search_ms": elapsed_ms(started_at),
            "web_search_query_count": len(payload.queries),
        }
        if payload.topic:
            metrics["web_search_topic"] = payload.topic
        if payload.time_range:
            metrics["web_search_time_range"] = payload.time_range
        return _result_from_prefixes(
            result,
            metrics,
            success_prefixes=("Tavily web results for:",),
            empty_prefixes=("No web results found for:",),
        )

    async def _handle_stock_quote(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        symbols = parse_tool_symbol_list_arguments(arguments, max_items=10)
        if not symbols:
            return _error("Market quote failed because the `symbol` or `symbols` argument was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_stock_quote_tool(symbols=symbols)
        metrics = {
            "stock_quote_ms": elapsed_ms(started_at),
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
        }
        quote_status = str(metrics["stock_quote_status"])
        status = ToolStatus.OK if quote_status in {"ok", "mixed"} else ToolStatus.EMPTY if quote_status == "symbol_suggestions" else ToolStatus.ERROR
        return ToolExecutionResult(content=result, status=status, metrics=metrics)

    async def _handle_price_history(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_price_history_arguments(arguments)
        if payload is None:
            return _error(
                "Price history failed because the `symbol` argument was missing or invalid, "
                "or an optional interval/outputsize value was invalid."
            )
        started_at = time.perf_counter()
        result = await self._execute_price_history_tool(
            symbol=payload.symbol,
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
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_annual_performance_arguments(arguments)
        if payload is None:
            return _error("Annual performance failed because `symbols` or `start_year` was invalid.")
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
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_channel_context_arguments(arguments)
        if payload is None:
            return _error("Channel context fetch failed because `mode`, `multiplier`, or `expand` was invalid.")
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
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        query = parse_tool_query_argument(arguments)
        if not query:
            return _error("Image search failed because the query argument was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_image_search_tool(query=query)
        return _result_from_prefixes(result, {
            "image_search_ms": elapsed_ms(started_at),
            "image_search_query_count": 1,
        }, success_prefixes=("Tavily image results for:",), empty_prefixes=("No image results found for:",))

    async def _handle_url_extract(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_extract_url_arguments(arguments)
        if payload is None:
            return _error("URL extraction failed because the `url` argument was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_extract_url_tool(url=payload.url, query=payload.query)
        return _result_from_prefixes(result, {
            "url_extract_ms": elapsed_ms(started_at),
            "url_extract_count": 1,
            "url_extract_provider": "browser" if result.startswith("Browser extract for:") else "tavily",
        }, success_prefixes=("Tavily extract for:", "Browser extract for:"), empty_prefixes=("No extractable content found for:",))

    async def _handle_browser_extract(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_browser_extract_arguments(arguments)
        if payload is None:
            return _error("Browser extract failed because `url`, `query`, or `headed` was invalid.")
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
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_youtube_transcript_arguments(arguments)
        if payload is None:
            return _error("YouTube transcript extraction failed because the `url` argument was missing or invalid.")
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
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        code = parse_python_exec_arguments(arguments)
        if code is None:
            return _error("Python execution failed because `code` was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_python_tool(code=code)
        return _result_from_prefixes(result, {
            "python_exec_ms": elapsed_ms(started_at),
            "python_exec_count": 1,
            "python_exec_status": "ok" if result.startswith("Python result") else "error",
        }, success_prefixes=("Python result",))

    async def _handle_create_reminder(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_create_reminder_arguments(arguments)
        if payload is None:
            return _error("Reminder creation failed because `message` or `remind_at` was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_create_reminder_tool(
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            user_id=context.user_id,
            source_message_id=context.source_message_id,
            reminder_text=payload.message,
            remind_at_text=payload.remind_at,
        )
        return _result_from_prefixes(result, {
            "reminder_create_ms": elapsed_ms(started_at),
            "reminder_create_count": 1,
        }, success_prefixes=("Reminder `",))

    async def _handle_send_message(
        self,
        arguments: str,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        payload = parse_send_channel_message_arguments(arguments)
        if payload is None:
            return _error("Channel send failed because `channel` or `message` was missing or invalid.")
        started_at = time.perf_counter()
        result = await self._execute_send_channel_message_tool(
            guild_id=context.guild_id,
            channel_target=payload.channel,
            message_text=payload.message,
            idempotency_key=context.action_key("send_msg", arguments),
        )
        return _result_from_prefixes(result, {
            "channel_send_ms": elapsed_ms(started_at),
            "channel_send_count": 1,
        }, success_prefixes=("Sent message to ", "Message to "))


def _error(content: str, *, retryable: bool = False) -> ToolExecutionResult:
    return ToolExecutionResult(
        content=content,
        status=ToolStatus.ERROR,
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
