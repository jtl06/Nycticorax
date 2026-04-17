from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

MAX_MODEL_ROWS = 5
MAX_CATEGORY_ROWS = 6
MAX_MODEL_CATEGORY_ROWS = 8
MAX_TOOL_ROWS = 6
MAX_RECENT_TOOL_ROWS = 5
CONTEXT_FEATURES = (
    "chat_reply",
    "chat_reply_final",
    "extended_context_summary",
    "vision_context",
)


@dataclass(slots=True)
class UsageModelRow:
    model: str
    event_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(slots=True)
class UsageCategoryRow:
    category: str
    event_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(slots=True)
class UsageModelCategoryRow:
    model: str
    category: str
    total_tokens: int


@dataclass(slots=True)
class ToolRow:
    tool_name: str
    event_count: int
    ok_count: int
    error_count: int
    empty_count: int
    avg_latency_ms: int


@dataclass(slots=True)
class RecentToolRow:
    tool_name: str
    status: str
    latency_ms: int
    created_at: datetime


@dataclass(slots=True)
class UsageLogsSnapshot:
    usage_event_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    context_prompt_tokens: int
    context_completion_tokens: int
    context_total_tokens: int
    model_rows: list[UsageModelRow]
    category_rows: list[UsageCategoryRow]
    model_category_rows: list[UsageModelCategoryRow]
    tool_rows: list[ToolRow]
    recent_tool_rows: list[RecentToolRow]


async def build_usage_logs_snapshot(
    session: AsyncSession,
    *,
    since: datetime,
    guild_id: int | None,
) -> UsageLogsSnapshot:
    from nycti.db.models import ToolCallEvent, UsageEvent

    usage_filters = _usage_filters(since=since, guild_id=guild_id)
    tool_filters = _tool_filters(since=since, guild_id=guild_id)

    usage_totals_raw = (
        await session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            ).where(*usage_filters)
        )
    ).one()

    context_totals_raw = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            ).where(*usage_filters, UsageEvent.feature.in_(CONTEXT_FEATURES))
        )
    ).one()

    model_rows_raw = (
        await session.execute(
            select(
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            )
            .where(*usage_filters)
            .group_by(UsageEvent.model)
            .order_by(desc(func.sum(UsageEvent.total_tokens)))
            .limit(MAX_MODEL_ROWS)
        )
    ).all()

    category_rows_raw = (
        await session.execute(
            select(
                UsageEvent.feature,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            )
            .where(*usage_filters)
            .group_by(UsageEvent.feature)
            .order_by(desc(func.sum(UsageEvent.total_tokens)))
            .limit(MAX_CATEGORY_ROWS)
        )
    ).all()

    model_category_rows_raw = (
        await session.execute(
            select(
                UsageEvent.model,
                UsageEvent.feature,
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
            )
            .where(*usage_filters)
            .group_by(UsageEvent.model, UsageEvent.feature)
            .order_by(desc(func.sum(UsageEvent.total_tokens)))
            .limit(MAX_MODEL_CATEGORY_ROWS)
        )
    ).all()

    tool_rows_raw = (
        await session.execute(
            select(
                ToolCallEvent.tool_name,
                func.count(ToolCallEvent.id),
                func.coalesce(func.sum(case((ToolCallEvent.status == "ok", 1), else_=0)), 0),
                func.coalesce(func.sum(case((ToolCallEvent.status == "error", 1), else_=0)), 0),
                func.coalesce(func.sum(case((ToolCallEvent.status == "empty", 1), else_=0)), 0),
                func.coalesce(func.avg(ToolCallEvent.latency_ms), 0.0),
            )
            .where(*tool_filters)
            .group_by(ToolCallEvent.tool_name)
            .order_by(desc(func.count(ToolCallEvent.id)))
            .limit(MAX_TOOL_ROWS)
        )
    ).all()

    recent_tool_rows_raw = (
        await session.execute(
            select(
                ToolCallEvent.tool_name,
                ToolCallEvent.status,
                ToolCallEvent.latency_ms,
                ToolCallEvent.created_at,
            )
            .where(*tool_filters)
            .order_by(desc(ToolCallEvent.created_at))
            .limit(MAX_RECENT_TOOL_ROWS)
        )
    ).all()

    return UsageLogsSnapshot(
        usage_event_count=int(usage_totals_raw[0] or 0),
        prompt_tokens=int(usage_totals_raw[1] or 0),
        completion_tokens=int(usage_totals_raw[2] or 0),
        total_tokens=int(usage_totals_raw[3] or 0),
        context_prompt_tokens=int(context_totals_raw[0] or 0),
        context_completion_tokens=int(context_totals_raw[1] or 0),
        context_total_tokens=int(context_totals_raw[2] or 0),
        model_rows=[
            UsageModelRow(
                model=str(model),
                event_count=int(event_count or 0),
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                total_tokens=int(total_tokens or 0),
            )
            for model, event_count, prompt_tokens, completion_tokens, total_tokens in model_rows_raw
        ],
        category_rows=[
            UsageCategoryRow(
                category=str(category),
                event_count=int(event_count or 0),
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                total_tokens=int(total_tokens or 0),
            )
            for category, event_count, prompt_tokens, completion_tokens, total_tokens in category_rows_raw
        ],
        model_category_rows=[
            UsageModelCategoryRow(
                model=str(model),
                category=str(category),
                total_tokens=int(total_tokens or 0),
            )
            for model, category, total_tokens in model_category_rows_raw
        ],
        tool_rows=[
            ToolRow(
                tool_name=str(tool_name),
                event_count=int(event_count or 0),
                ok_count=int(ok_count or 0),
                error_count=int(error_count or 0),
                empty_count=int(empty_count or 0),
                avg_latency_ms=round(float(avg_latency_ms or 0.0)),
            )
            for tool_name, event_count, ok_count, error_count, empty_count, avg_latency_ms in tool_rows_raw
        ],
        recent_tool_rows=[
            RecentToolRow(
                tool_name=str(tool_name),
                status=str(status),
                latency_ms=int(latency_ms or 0),
                created_at=_coerce_datetime(created_at),
            )
            for tool_name, status, latency_ms, created_at in recent_tool_rows_raw
        ],
    )


def format_usage_logs_report(
    snapshot: UsageLogsSnapshot,
    *,
    window_label: str,
    now: datetime | None = None,
) -> str:
    current_now = now or datetime.now(timezone.utc)
    context_share = (
        round((snapshot.context_total_tokens / snapshot.total_tokens) * 100.0, 1)
        if snapshot.total_tokens > 0
        else 0.0
    )
    lines: list[str] = [
        f"Usage logs for `{window_label}`",
        (
            f"LLM events `{snapshot.usage_event_count}` | prompt `{snapshot.prompt_tokens:,}` | "
            f"completion `{snapshot.completion_tokens:,}` | total `{snapshot.total_tokens:,}`"
        ),
        (
            f"Context bandwidth: total `{snapshot.context_total_tokens:,}` ({context_share}%), "
            f"prompt `{snapshot.context_prompt_tokens:,}`, completion `{snapshot.context_completion_tokens:,}`"
        ),
        "",
        "By model:",
    ]
    if snapshot.model_rows:
        lines.extend(
            [
                (
                    f"- `{_compact_model_name(row.model)}`: events `{row.event_count}`, "
                    f"prompt `{row.prompt_tokens:,}`, completion `{row.completion_tokens:,}`, "
                    f"total `{row.total_tokens:,}`"
                )
                for row in snapshot.model_rows
            ]
        )
    else:
        lines.append("- (none)")

    lines.extend(["", "By category (feature):"])
    if snapshot.category_rows:
        lines.extend(
            [
                (
                    f"- `{row.category}`: events `{row.event_count}`, prompt `{row.prompt_tokens:,}`, "
                    f"completion `{row.completion_tokens:,}`, total `{row.total_tokens:,}`"
                )
                for row in snapshot.category_rows
            ]
        )
    else:
        lines.append("- (none)")

    lines.extend(["", "By model + category:"])
    if snapshot.model_category_rows:
        lines.extend(
            [
                f"- `{_compact_model_name(row.model)}` + `{row.category}`: total `{row.total_tokens:,}`"
                for row in snapshot.model_category_rows
            ]
        )
    else:
        lines.append("- (none)")

    lines.extend(["", "Tool calls:"])
    if snapshot.tool_rows:
        lines.extend(
            [
                (
                    f"- `{row.tool_name}`: calls `{row.event_count}` "
                    f"(ok `{row.ok_count}`, error `{row.error_count}`, empty `{row.empty_count}`), "
                    f"avg `{row.avg_latency_ms}ms`"
                )
                for row in snapshot.tool_rows
            ]
        )
    else:
        lines.append("- (none)")

    if snapshot.recent_tool_rows:
        lines.extend(["", "Recent tool calls:"])
        lines.extend(
            [
                (
                    f"- `{row.tool_name}` `{row.status}` `{row.latency_ms}ms` "
                    f"({_format_age(current_now, row.created_at)})"
                )
                for row in snapshot.recent_tool_rows
            ]
        )

    rendered = "\n".join(lines).strip()
    if len(rendered) <= 1900:
        return rendered
    return rendered[:1897].rstrip() + "..."


def register_logs_command(bot: Any, *, guild: Any = None) -> None:
    @bot.tree.command(name="logs", description="Show recent model/token/tool usage logs.", guild=guild)
    @app_commands.describe(
        period="Window preset: day, week, or custom",
        hours="Used only when period is `custom` (1-720)",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="day", value="day"),
            app_commands.Choice(name="week", value="week"),
            app_commands.Choice(name="custom", value="custom"),
        ],
    )
    async def logs_command(
        interaction: discord.Interaction,
        period: str = "day",
        hours: app_commands.Range[int, 1, 720] | None = None,
    ) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to view logs.",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc)
        since, window_label = _resolve_window(
            period=period,
            hours=hours,
            now=now,
        )

        async with bot.database.session() as session:
            snapshot = await build_usage_logs_snapshot(
                session,
                since=since,
                guild_id=interaction.guild.id,
            )

        report = format_usage_logs_report(
            snapshot,
            window_label=window_label,
            now=now,
        )
        await interaction.response.send_message(report, ephemeral=True)


def _usage_filters(*, since: datetime, guild_id: int | None) -> list[object]:
    from nycti.db.models import UsageEvent

    filters: list[object] = [UsageEvent.created_at >= since]
    if guild_id is not None:
        filters.append(UsageEvent.guild_id == guild_id)
    return filters


def _tool_filters(*, since: datetime, guild_id: int | None) -> list[object]:
    from nycti.db.models import ToolCallEvent

    filters: list[object] = [ToolCallEvent.created_at >= since]
    if guild_id is not None:
        filters.append(ToolCallEvent.guild_id == guild_id)
    return filters


def _compact_model_name(model: str) -> str:
    normalized = (model or "").strip()
    if not normalized:
        return "(unknown)"
    lower_value = normalized.casefold()
    prefix = "https://clarifai.com/"
    if lower_value.startswith(prefix):
        marker = "/models/"
        marker_index = lower_value.find(marker)
        if marker_index >= 0:
            start = marker_index + len(marker)
            tail = normalized[start:]
            model_name = tail.split("/", 1)[0]
        else:
            model_name = normalized.rsplit("/", 1)[-1]
        compact = model_name.strip().casefold()
        compact = re.sub(r"(?<=\d)_(?=\d)", ".", compact)
        compact = compact.replace("_", "-")
        return f"clarifai {compact}" if compact else "clarifai"
    return normalized


def _resolve_window(
    *,
    period: str,
    hours: int | None,
    now: datetime,
) -> tuple[datetime, str]:
    if period == "week":
        return now - timedelta(days=7), "last 7d"
    if period == "custom":
        custom_hours = hours or 24
        return now - timedelta(hours=custom_hours), f"last {custom_hours}h"
    return now - timedelta(hours=24), "last 24h"


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _format_age(now: datetime, created_at: datetime) -> str:
    elapsed = max((now - created_at).total_seconds(), 0.0)
    if elapsed < 60:
        return f"{round(elapsed)}s ago"
    if elapsed < 3600:
        return f"{round(elapsed / 60)}m ago"
    if elapsed < 86400:
        return f"{round(elapsed / 3600)}h ago"
    return f"{round(elapsed / 86400)}d ago"
