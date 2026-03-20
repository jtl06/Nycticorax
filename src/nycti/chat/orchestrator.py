from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord

from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.formatting import (
    extract_think_content,
    format_discord_message_link,
    parse_json_object_payload,
)
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
from nycti.timezones import get_timezone
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
MAX_CHAT_TOOL_ITERATIONS = 4


class ChatOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        llm_client: OpenAIClient,
        tavily_client: TavilyClient,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
        bot: discord.Client,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self.bot = bot

    async def run_chat_with_tools(
        self,
        *,
        session,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        search_requested: bool,
        metrics: dict[str, int | str] | None,
    ) -> tuple[str, list[str]]:
        tools = self._build_chat_tools()
        required_tools: set[str] = set()
        if search_requested:
            required_tools.add("web_search")
        used_tools: set[str] = set()
        latest_tool_results: list[str] = []
        reasoning_parts: list[str] = []
        if metrics is not None:
            metrics["tool_call_count"] = 0
        for _ in range(MAX_CHAT_TOOL_ITERATIONS + 1):
            chat_started_at = time.perf_counter()
            turn = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_chat_model,
                feature="chat_reply",
                max_tokens=self.settings.max_completion_tokens,
                temperature=0.7,
                messages=messages,
                tools=tools,
            )
            if metrics is not None:
                metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(chat_started_at)
                metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
                metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
                metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
                _append_raw_tool_trace(metrics, turn.raw_text)
            reasoning_parts.extend(_collect_reasoning(turn))
            usage_write_started_at = time.perf_counter()
            await record_usage(
                session,
                usage=turn.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            if metrics is not None:
                metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + _elapsed_ms(
                    usage_write_started_at
                )
            if not turn.tool_calls:
                missing_required_tools = required_tools - used_tools
                if missing_required_tools:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Before answering, you still must call these tools at least once: "
                                + ", ".join(sorted(missing_required_tools))
                            ),
                        }
                    )
                    continue
                if turn.text:
                    return turn.text, reasoning_parts
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        }
                        for tool_call in turn.tool_calls
                    ],
                }
            )
            used_tools.update(tool_call.name for tool_call in turn.tool_calls)
            if metrics is not None:
                metrics["tool_call_count"] = int(metrics.get("tool_call_count", 0)) + len(turn.tool_calls)
            tool_results = await asyncio.gather(
                *[
                    self._execute_chat_tool_call(
                        session=session,
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        source_message_id=source_message_id,
                    )
                    for tool_call in turn.tool_calls
                ]
            )
            rendered_tool_results: list[str] = []
            for tool_call, (tool_result, tool_metrics) in zip(turn.tool_calls, tool_results):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )
                rendered_tool_results.append(f"{tool_call.name}:\n{tool_result}")
                latest_tool_results.append(tool_result)
                if metrics is not None:
                    for key, value in tool_metrics.items():
                        metrics[key] = int(metrics.get(key, 0)) + value
            if rendered_tool_results:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool results for continuation:\n"
                            + "\n\n".join(rendered_tool_results)
                            + "\n\nUse these results. Only call another tool if you still need one."
                        ),
                    }
                )

        text, final_reasoning = await self._force_final_answer(
            session=session,
            messages=messages,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            metrics=metrics,
            latest_tool_results=latest_tool_results,
        )
        reasoning_parts.extend(final_reasoning)
        return text, reasoning_parts

    async def _force_final_answer(
        self,
        *,
        session,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        latest_tool_results: list[str],
    ) -> tuple[str, list[str]]:
        final_messages = list(messages)
        final_messages.append(
            {
                "role": "user",
                "content": (
                    "Stop using tools now. Give the final answer directly from the tool results and context you already have."
                ),
            }
        )
        chat_started_at = time.perf_counter()
        turn = await self.llm_client.complete_chat_turn(
            model=self.settings.openai_chat_model,
            feature="chat_reply_final",
            max_tokens=self.settings.max_completion_tokens,
            temperature=0.4,
            messages=final_messages,
            tools=None,
        )
        if metrics is not None:
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(chat_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
            _append_raw_tool_trace(metrics, turn.raw_text)
        reasoning_parts = _collect_reasoning(turn)
        usage_write_started_at = time.perf_counter()
        await record_usage(
            session,
            usage=turn.usage,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        if metrics is not None:
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + _elapsed_ms(
                usage_write_started_at
            )
        if turn.text:
            return turn.text, reasoning_parts
        if latest_tool_results:
            return latest_tool_results[-1], reasoning_parts
        return "I hit the tool-call limit for this reply. Try asking in a more focused way.", reasoning_parts

    def _build_chat_tools(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "Search the web for fresh public information and source snippets. "
                        "Prefer one comprehensive query first. Only issue another search if earlier results are insufficient or conflicting."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The focused web search query to run.",
                            }
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_reminder",
                    "description": (
                        "Create a future reminder for the current user in this channel. "
                        "Use this when the user asks to be reminded on a specific date or time. "
                        "Prefer ISO 8601 date-times with timezone offsets. Date-only values are allowed and default to 09:00 local time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "The short reminder text to send later.",
                            },
                            "remind_at": {
                                "type": "string",
                                "description": (
                                    "When to send the reminder. Use an ISO 8601 local date or date-time, "
                                    "for example 2026-03-22 or 2026-03-22T15:30:00-07:00."
                                ),
                            },
                        },
                        "required": ["message", "remind_at"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_channel_message",
                    "description": (
                        "Send a message into another channel in the current Discord server. "
                        "Use a configured channel alias or a numeric channel ID. "
                        "Only use this when the user explicitly wants you to post somewhere else."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "Known channel alias or numeric channel ID.",
                            },
                            "message": {
                                "type": "string",
                                "description": "The message to send into that channel.",
                            },
                        },
                        "required": ["channel", "message"],
                    },
                },
            },
        ]

    async def _execute_chat_tool_call(
        self,
        *,
        session,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, dict[str, int]]:
        query = _parse_tool_query_argument(arguments)
        if tool_name == "web_search":
            if not query:
                return "Tool call failed because the query argument was missing or invalid.", {}
            started_at = time.perf_counter()
            result = await self._execute_web_search_tool(query=query)
            return result, {
                "web_search_ms": _elapsed_ms(started_at),
                "web_search_query_count": 1,
            }
        if tool_name == "create_reminder":
            payload = _parse_create_reminder_arguments(arguments)
            if payload is None:
                return "Reminder creation failed because `message` or `remind_at` was missing or invalid.", {}
            reminder_text, remind_at_text = payload
            started_at = time.perf_counter()
            result = await self._execute_create_reminder_tool(
                session=session,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                reminder_text=reminder_text,
                remind_at_text=remind_at_text,
            )
            return result, {
                "reminder_create_ms": _elapsed_ms(started_at),
                "reminder_create_count": 1,
            }
        if tool_name == "send_channel_message":
            payload = _parse_send_channel_message_arguments(arguments)
            if payload is None:
                return "Channel send failed because `channel` or `message` was missing or invalid.", {}
            channel_target, message_text = payload
            started_at = time.perf_counter()
            result = await self._execute_send_channel_message_tool(
                session=session,
                guild_id=guild_id,
                channel_target=channel_target,
                message_text=message_text,
            )
            return result, {
                "channel_send_ms": _elapsed_ms(started_at),
                "channel_send_count": 1,
            }
        return f"Unknown tool `{tool_name}`.", {}

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

    async def _execute_create_reminder_tool(
        self,
        *,
        session,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        reminder_text: str,
        remind_at_text: str,
    ) -> str:
        if channel_id is None:
            return "Reminder creation failed because this channel could not be resolved."
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
        session,
        guild_id: int | None,
        channel_target: str,
        message_text: str,
    ) -> str:
        if guild_id is None:
            return "Channel send failed because this request was not tied to a server."
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


def _collect_reasoning(turn) -> list[str]:
    parts: list[str] = []
    if turn.reasoning_content:
        parts.append(turn.reasoning_content)
    inline_think = extract_think_content(turn.raw_text)
    parts.extend(inline_think)
    return parts


def _append_raw_tool_trace(metrics: dict[str, int | str], raw_text: str) -> None:
    cleaned = raw_text.strip()
    if not cleaned or "<|tool_call" not in cleaned:
        return
    existing = str(metrics.get("raw_tool_trace", "")).strip()
    if existing:
        metrics["raw_tool_trace"] = existing + "\n\n---\n\n" + cleaned
        return
    metrics["raw_tool_trace"] = cleaned


def _parse_tool_query_argument(arguments: str) -> str | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    query = str(payload.get("query", "")).strip()
    return query or None


def _parse_create_reminder_arguments(arguments: str) -> tuple[str, str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    reminder_text = str(payload.get("message", "")).strip()
    remind_at_text = str(payload.get("remind_at", "")).strip()
    if not reminder_text or not remind_at_text:
        return None
    return reminder_text, remind_at_text


def _parse_send_channel_message_arguments(arguments: str) -> tuple[str, str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    channel_target = str(payload.get("channel", "")).strip()
    message_text = str(payload.get("message", "")).strip()
    if not channel_target or not message_text:
        return None
    return channel_target, message_text


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
