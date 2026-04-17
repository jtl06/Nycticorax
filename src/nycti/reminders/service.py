from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Reminder
from nycti.reminders.parsing import ParsedReminderTime, parse_remind_at


class ReminderService:
    async def create_reminder(
        self,
        session: AsyncSession,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        source_message_id: int | None,
        reminder_text: str,
        remind_at: datetime,
    ) -> Reminder:
        reminder = Reminder(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            reminder_text=reminder_text,
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.flush()
        return reminder

    async def list_due_reminders(
        self,
        session: AsyncSession,
        *,
        due_before: datetime,
        limit: int = 25,
    ) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(
                Reminder.delivered_at.is_(None),
                Reminder.remind_at <= due_before,
            )
            .order_by(Reminder.remind_at.asc(), Reminder.id.asc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    async def list_pending_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        limit: int = 20,
    ) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(
                Reminder.user_id == user_id,
                Reminder.delivered_at.is_(None),
            )
            .order_by(Reminder.remind_at.asc(), Reminder.id.asc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    async def list_pending_for_guild(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        limit: int = 50,
    ) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(
                Reminder.guild_id == guild_id,
                Reminder.delivered_at.is_(None),
            )
            .order_by(Reminder.remind_at.asc(), Reminder.id.asc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    async def delete_reminder(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        reminder_id: int,
    ) -> bool:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.user_id != user_id or reminder.delivered_at is not None:
            return False
        await session.delete(reminder)
        await session.flush()
        return True

    async def mark_delivered(
        self,
        session: AsyncSession,
        reminder: Reminder,
        *,
        delivered_at: datetime,
    ) -> None:
        reminder.delivered_at = delivered_at
        await session.flush()

    async def prune_delivered_before(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
    ) -> int:
        result = await session.execute(
            delete(Reminder).where(
                Reminder.delivered_at.is_not(None),
                Reminder.delivered_at < cutoff,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def parse_remind_at(value: str, *, now: datetime) -> ParsedReminderTime | None:
        return parse_remind_at(value, now=now)
