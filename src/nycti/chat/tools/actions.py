from __future__ import annotations

from datetime import datetime, timezone

from nycti.formatting import format_discord_message_link
from nycti.timezones import get_timezone


class ActionToolMixin:
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
        idempotency_key: str,
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
        if not await self._claim_send_once(idempotency_key):
            return f"Message to <#{resolved_channel_id}> was already sent for this request."
        try:
            await channel.send(message_text)
        except Exception:
            await self._release_send_claim(idempotency_key)
            return f"Channel send failed because the bot could not send to `{channel_target}`."
        return f"Sent message to <#{resolved_channel_id}>."

    async def _claim_send_once(self, idempotency_key: str) -> bool:
        state_key = f"send_once:{idempotency_key[:40]}"
        if state_key in self._claimed_action_keys:
            return False
        if not hasattr(self.database, "session"):
            self._claimed_action_keys.add(state_key)
            return True
        try:
            from sqlalchemy.exc import IntegrityError

            from nycti.db.models import AppState

            async with self.database.session() as session:
                existing = await session.get(AppState, state_key)
                if existing is not None:
                    return False
                session.add(AppState(key=state_key, value=datetime.now(timezone.utc).isoformat()))
                await session.commit()
        except IntegrityError:
            return False
        self._claimed_action_keys.add(state_key)
        return True

    async def _release_send_claim(self, idempotency_key: str) -> None:
        state_key = f"send_once:{idempotency_key[:40]}"
        self._claimed_action_keys.discard(state_key)
        if not hasattr(self.database, "session"):
            return
        try:
            from sqlalchemy import delete

            from nycti.db.models import AppState

            async with self.database.session() as session:
                await session.execute(delete(AppState).where(AppState.key == state_key))
                await session.commit()
        except Exception:
            return
