from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    import discord as _discord
except ModuleNotFoundError:  # pragma: no cover - production installs discord.py
    _discord = None

discord: Any = _discord

from nycti.chat.action_confirmation import (
    ActionConfirmationStore,
    ActionKind,
    ActionProposal,
    ChannelMessageAction,
    ReminderAction,
    render_action_proposal_card,
)
from nycti.discord.channel_access import member_can_send_to_channel
from nycti.formatting import format_discord_message_link
from nycti.timezones import get_timezone

MAX_REMINDER_TEXT_CHARS = 500
MAX_CHANNEL_MESSAGE_CHARS = 1_900


class ActionToolMixin:
    action_confirmations: ActionConfirmationStore
    bot: Any
    channel_alias_service: Any
    database: Any
    memory_service: Any
    reminder_service: Any
    _claimed_action_keys: set[str]

    async def _propose_create_reminder_tool(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        reminder_text: str,
        remind_at_text: str,
    ) -> str:
        if guild_id is None or channel_id is None:
            return "Reminder proposal failed because this request was not tied to a server channel."
        request_channel = await self._get_action_channel(channel_id)
        if request_channel is None or not await self._channel_send_is_authorized(
            request_channel,
            guild_id=guild_id,
            user_id=user_id,
        ):
            return (
                "Reminder proposal failed because this channel is unavailable or the requester/bot "
                "cannot view and send here."
            )
        cleaned_text = " ".join(reminder_text.split()).strip()
        if not cleaned_text or len(cleaned_text) > MAX_REMINDER_TEXT_CHARS:
            return f"Reminder proposal failed because the message must be 1-{MAX_REMINDER_TEXT_CHARS} characters."

        async with self.database.session() as session:
            timezone_name = await self.memory_service.get_timezone_name(session, user_id)
        user_timezone = get_timezone(timezone_name)
        parsed = self.reminder_service.parse_remind_at(
            remind_at_text,
            now=datetime.now(timezone.utc).astimezone(user_timezone),
        )
        if parsed is None:
            return (
                "Reminder proposal failed because `remind_at` was invalid. "
                "Use an ISO 8601 local date or date-time, like `2026-03-22` or "
                "`2026-03-22T15:30:00-07:00`."
            )
        remind_at_utc = parsed.remind_at.astimezone(timezone.utc)
        if remind_at_utc <= datetime.now(timezone.utc):
            return "Reminder proposal failed because the requested time is not in the future."

        local_remind_at = parsed.remind_at.astimezone(user_timezone)
        proposal = await self.action_confirmations.propose(
            kind=ActionKind.CREATE_REMINDER,
            payload=ReminderAction(
                reminder_text=cleaned_text,
                remind_at_utc=remind_at_utc,
                timezone_name=timezone_name,
                local_remind_at_text=local_remind_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
                assumed_time=parsed.assumed_time,
            ),
            guild_id=guild_id,
            request_channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
        )
        return render_action_proposal_card(proposal)

    async def _propose_send_channel_message_tool(
        self,
        *,
        guild_id: int | None,
        request_channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        channel_target: str,
        message_text: str,
    ) -> str:
        if guild_id is None or request_channel_id is None:
            return "Channel-send proposal failed because this request was not tied to a server channel."
        cleaned_message = message_text.strip()
        if not cleaned_message or len(cleaned_message) > MAX_CHANNEL_MESSAGE_CHARS:
            return (
                "Channel-send proposal failed because the message must be "
                f"1-{MAX_CHANNEL_MESSAGE_CHARS} characters."
            )
        resolved_channel_id = await self._resolve_action_channel(
            guild_id=guild_id,
            user_id=user_id,
            channel_target=channel_target,
        )
        if resolved_channel_id is None:
            return (
                "Channel-send proposal failed because that target is unknown, outside this server, "
                "or unavailable to the requester/bot."
            )
        if resolved_channel_id == request_channel_id:
            return (
                "Channel-send proposal was not created because the target is the current channel. "
                "Reply directly in the current response instead; use exact mapped `<@...>` tokens "
                "when the user asked to ping someone."
            )

        proposal = await self.action_confirmations.propose(
            kind=ActionKind.SEND_CHANNEL_MESSAGE,
            payload=ChannelMessageAction(
                target_channel_id=resolved_channel_id,
                message_text=cleaned_message,
            ),
            guild_id=guild_id,
            request_channel_id=request_channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
        )
        return render_action_proposal_card(proposal)

    async def confirm_action(
        self,
        proposal_id: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> str:
        capability = await self.action_confirmations.confirm(
            proposal_id,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        proposal = await self.action_confirmations.consume(
            capability.token,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        if proposal.kind == ActionKind.CREATE_REMINDER:
            return await self._execute_confirmed_reminder(proposal)
        if proposal.kind == ActionKind.SEND_CHANNEL_MESSAGE:
            return await self._execute_confirmed_channel_message(proposal)
        raise RuntimeError(f"Unsupported confirmed action: {proposal.kind}")

    async def _execute_confirmed_reminder(self, proposal: ActionProposal) -> str:
        payload = proposal.payload
        if not isinstance(payload, ReminderAction):
            raise RuntimeError("Reminder proposal payload type mismatch.")
        if payload.remind_at_utc <= datetime.now(timezone.utc):
            return (
                "Reminder creation stopped because the confirmed time has already passed. "
                "Request a new proposal with a future time."
            )
        request_channel = await self._get_action_channel(proposal.request_channel_id)
        if request_channel is None or not await self._channel_send_is_authorized(
            request_channel,
            guild_id=proposal.guild_id,
            user_id=proposal.user_id,
        ):
            return (
                "Reminder creation stopped because the original channel is unavailable or the "
                "requester/bot can no longer view and send there."
            )
        async with self.database.session() as session:
            reminder = await self.reminder_service.create_reminder(
                session,
                guild_id=proposal.guild_id,
                channel_id=proposal.request_channel_id,
                user_id=proposal.user_id,
                source_message_id=proposal.source_message_id,
                reminder_text=payload.reminder_text,
                remind_at=payload.remind_at_utc,
            )
            await session.commit()
        reminder_line = (
            f"Reminder `{reminder.id}` created for {payload.local_remind_at_text}: "
            f"{reminder.reminder_text}"
        )
        if payload.assumed_time:
            reminder_line += " (assumed 09:00 local time because only a date was provided)"
        if proposal.source_message_id is not None:
            jump_link = format_discord_message_link(
                guild_id=proposal.guild_id,
                channel_id=proposal.request_channel_id,
                message_id=proposal.source_message_id,
            )
            reminder_line += f"\nOriginal message: {jump_link}"
        return reminder_line

    async def _execute_confirmed_channel_message(self, proposal: ActionProposal) -> str:
        payload = proposal.payload
        if not isinstance(payload, ChannelMessageAction):
            raise RuntimeError("Channel-message proposal payload type mismatch.")
        channel = await self._get_action_channel(payload.target_channel_id)
        if channel is None:
            return f"Channel send stopped because channel `{payload.target_channel_id}` could not be fetched."
        if not await self._channel_send_is_authorized(
            channel,
            guild_id=proposal.guild_id,
            user_id=proposal.user_id,
        ):
            return (
                "Channel send stopped because the confirmed target is outside this server or the "
                "requester/bot can no longer view and send there."
            )
        if not await self._claim_send_once(proposal.proposal_id):
            return f"Message to <#{payload.target_channel_id}> was already sent for this proposal."
        try:
            send_kwargs: dict[str, object] = {}
            if discord is not None:
                send_kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            sent_message = await channel.send(payload.message_text, **send_kwargs)
        except Exception:
            # Discord may have accepted the message before the client observed
            # an error.  Retain the at-most-once claim and report uncertainty
            # instead of inviting an unsafe retry.
            return (
                f"Message status for <#{payload.target_channel_id}> is unknown. Check the target channel "
                "before requesting another send."
            )
        receipt = getattr(sent_message, "jump_url", "") or getattr(sent_message, "id", "")
        suffix = f" Receipt: {receipt}." if receipt else ""
        return f"Sent confirmed message to <#{payload.target_channel_id}>.{suffix}"

    async def _resolve_action_channel(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_target: str,
    ) -> int | None:
        cleaned_target = channel_target.strip().removeprefix("<#").removesuffix(">")
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
            return None
        channel = await self._get_action_channel(resolved_channel_id)
        if channel is None or not await self._channel_send_is_authorized(
            channel,
            guild_id=guild_id,
            user_id=user_id,
        ):
            return None
        send = getattr(channel, "send", None)
        return resolved_channel_id if callable(send) else None

    async def _get_action_channel(self, channel_id: int) -> Any | None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        fetch_channel = getattr(self.bot, "fetch_channel", None)
        if not callable(fetch_channel):
            return None
        try:
            return await fetch_channel(channel_id)
        except Exception:
            return None

    async def _channel_send_is_authorized(
        self,
        channel: Any,
        *,
        guild_id: int,
        user_id: int,
    ) -> bool:
        guild = getattr(channel, "guild", None)
        if guild is None or getattr(guild, "id", None) != guild_id:
            return False
        requester = await self._resolve_guild_member(guild, user_id)
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            bot_user = getattr(self.bot, "user", None)
            bot_user_id = getattr(bot_user, "id", None)
            if isinstance(bot_user_id, int):
                bot_member = await self._resolve_guild_member(guild, bot_user_id)
        if requester is None or bot_member is None:
            return False
        return bool(
            await member_can_send_to_channel(channel, requester)
            and await member_can_send_to_channel(channel, bot_member)
        )

    @staticmethod
    async def _resolve_guild_member(guild: Any, user_id: int) -> Any | None:
        get_member = getattr(guild, "get_member", None)
        member = get_member(user_id) if callable(get_member) else None
        if member is not None:
            return member
        fetch_member = getattr(guild, "fetch_member", None)
        if not callable(fetch_member):
            return None
        try:
            return await fetch_member(user_id)
        except Exception:
            return None

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
