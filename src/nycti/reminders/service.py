from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
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

    async def mark_delivered(
        self,
        session: AsyncSession,
        reminder: Reminder,
        *,
        delivered_at: datetime,
    ) -> None:
        reminder.delivered_at = delivered_at
        await session.flush()

    @staticmethod
    def parse_remind_at(value: str, *, now: datetime) -> ParsedReminderTime | None:
        return parse_remind_at(value, now=now)
