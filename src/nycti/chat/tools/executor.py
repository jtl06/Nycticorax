from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from nycti.channel_aliases import ChannelAliasService
from nycti.chat.tools.parsing import (
    parse_create_reminder_arguments,
    parse_send_channel_message_arguments,
    parse_tool_query_argument,
)
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from nycti.db.session import Database
from nycti.formatting import format_discord_message_link
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.tavily.formatting import format_tavily_search_message
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
from nycti.timezones import get_timezone

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ChatToolExecutor:
    def __init__(
        self,
        *,
        database: Database,
        tavily_client: TavilyClient,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
        bot: discord.Client,
    ) -> None:
        self.database = database
        self.tavily_client = tavily_client
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
    ) -> tuple[str, dict[str, int]]:
        if tool_name == WEB_SEARCH_TOOL_NAME:
            query = parse_tool_query_argument(arguments)
            if not query:
                return "Tool call failed because the query argument was missing or invalid.", {}
            started_at = time.perf_counter()
            result = await self._execute_web_search_tool(query=query)
            return result, {
                "web_search_ms": _elapsed_ms(started_at),
                "web_search_query_count": 1,
            }

        if tool_name == CREATE_REMINDER_TOOL_NAME:
            payload = parse_create_reminder_arguments(arguments)
            if payload is None:
                return "Reminder creation failed because `message` or `remind_at` was missing or invalid.", {}
            started_at = time.perf_counter()
            result = await self._execute_create_reminder_tool(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                reminder_text=payload.message,
                remind_at_text=payload.remind_at,
            )
            return result, {
                "reminder_create_ms": _elapsed_ms(started_at),
                "reminder_create_count": 1,
            }

        if tool_name == SEND_CHANNEL_MESSAGE_TOOL_NAME:
            payload = parse_send_channel_message_arguments(arguments)
            if payload is None:
                return "Channel send failed because `channel` or `message` was missing or invalid.", {}
            started_at = time.perf_counter()
            result = await self._execute_send_channel_message_tool(
                guild_id=guild_id,
                channel_target=payload.channel,
                message_text=payload.message,
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
