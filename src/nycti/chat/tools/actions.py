from __future__ import annotations

from datetime import datetime, timezone

from nycti.formatting import format_discord_message_link
from nycti.message_context import format_message_line, message_has_visible_content
from nycti.memory.profile import should_attempt_profile_update
from nycti.timezones import get_timezone


class ActionToolMixin:
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
            except Exception:
                return f"Channel send failed because channel `{channel_target}` could not be fetched."
        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or channel_guild.id != guild_id:
            return "Channel send failed because the target channel is not in this server."
        try:
            await channel.send(message_text)
        except Exception:
            return f"Channel send failed because the bot could not send to `{channel_target}`."
        return f"Sent message to <#{resolved_channel_id}>."
