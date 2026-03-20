from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from nycti.db.models import ChannelAlias


class ChannelAliasService:
    async def set_alias(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        alias: str,
        channel_id: int,
    ) -> "ChannelAlias":
        from nycti.db.models import ChannelAlias

        normalized_alias = normalize_channel_alias(alias)
        if normalized_alias is None:
            raise ValueError("Alias must be 1-32 chars using letters, numbers, hyphens, or underscores.")
        existing = await session.scalar(
            select(ChannelAlias).where(
                ChannelAlias.guild_id == guild_id,
                ChannelAlias.alias == normalized_alias,
            )
        )
        if existing is not None:
            existing.channel_id = channel_id
            await session.flush()
            return existing
        alias_row = ChannelAlias(guild_id=guild_id, alias=normalized_alias, channel_id=channel_id)
        session.add(alias_row)
        await session.flush()
        return alias_row

    async def delete_alias(self, session: AsyncSession, *, guild_id: int, alias: str) -> bool:
        normalized_alias = normalize_channel_alias(alias)
        if normalized_alias is None:
            return False
        alias_row = await session.scalar(
            select(ChannelAlias).where(
                ChannelAlias.guild_id == guild_id,
                ChannelAlias.alias == normalized_alias,
            )
        )
        if alias_row is None:
            return False
        await session.delete(alias_row)
        await session.flush()
        return True

    async def list_aliases(self, session: AsyncSession, *, guild_id: int) -> list["ChannelAlias"]:
        from nycti.db.models import ChannelAlias

        stmt = (
            select(ChannelAlias)
            .where(ChannelAlias.guild_id == guild_id)
            .order_by(ChannelAlias.alias.asc(), ChannelAlias.channel_id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def resolve_channel_id(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        channel: str,
    ) -> int | None:
        from nycti.db.models import ChannelAlias

        cleaned = channel.strip()
        if cleaned.isdigit():
            return int(cleaned)
        normalized_alias = normalize_channel_alias(cleaned)
        if normalized_alias is None:
            return None
        alias_row = await session.scalar(
            select(ChannelAlias).where(
                ChannelAlias.guild_id == guild_id,
                ChannelAlias.alias == normalized_alias,
            )
        )
        if alias_row is None:
            return None
        return alias_row.channel_id


def normalize_channel_alias(value: str) -> str | None:
    cleaned = value.strip().lower()
    if not cleaned or len(cleaned) > 32:
        return None
    if not re.fullmatch(r"[a-z0-9_-]+", cleaned):
        return None
    return cleaned
