from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from nycti.discord.logs import build_usage_logs_snapshot, format_usage_logs_report

if TYPE_CHECKING:
    from nycti.config import Settings
    from nycti.db.session import Database

DAILY_LOG_SUMMARY_CHECK_SECONDS = 3600
DAILY_LOG_SUMMARY_MIN_INTERVAL_SECONDS = 86400
DAILY_LOG_SUMMARY_STATE_KEY = "daily_log_summary_posted_at"


async def build_daily_logs_summary(
    database: Database,
    *,
    guild_id: int | None,
    now: datetime | None = None,
) -> str:
    current_now = now or datetime.now(timezone.utc)
    since = current_now - timedelta(hours=24)
    async with database.session() as session:
        snapshot = await build_usage_logs_snapshot(
            session,
            since=since,
            guild_id=guild_id,
    )
    report = format_usage_logs_report(
        snapshot,
        window_label="last 24h",
        now=current_now,
    )
    return f"nycti_daily_logs_summary\n{report}"


async def post_daily_logs_summary_if_due(
    bot: Any,
    *,
    database: Database,
    settings: Settings,
) -> None:
    from nycti.db.models import AppState
    from nycti.error_debug import send_error_debug_message

    if settings.error_debug_channel_id is None:
        return
    now = datetime.now(timezone.utc)
    async with database.session() as session:
        state = await session.get(AppState, DAILY_LOG_SUMMARY_STATE_KEY)
        last_posted_value = state.value if state is not None else None
    if _posted_recently(last_posted_value, now=now):
        return
    summary = await build_daily_logs_summary(database, guild_id=settings.discord_guild_id, now=now)
    await send_error_debug_message(bot, channel_id=settings.error_debug_channel_id, content=summary)
    async with database.session() as session:
        state = await session.get(AppState, DAILY_LOG_SUMMARY_STATE_KEY)
        if state is None:
            session.add(AppState(key=DAILY_LOG_SUMMARY_STATE_KEY, value=now.isoformat()))
        else:
            state.value = now.isoformat()
        await session.commit()


def _posted_recently(value: str | None, *, now: datetime) -> bool:
    if value is None:
        return False
    try:
        posted_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    elapsed = (now - posted_at.astimezone(timezone.utc)).total_seconds()
    return elapsed < DAILY_LOG_SUMMARY_MIN_INTERVAL_SECONDS
