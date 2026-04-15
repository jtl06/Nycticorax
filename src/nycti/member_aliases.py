from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from nycti.db.models import MemberAlias


MAX_ALIAS_LENGTH = 40
MAX_NOTE_LENGTH = 160


class MemberAliasService:
    async def set_alias(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        user_id: int,
        alias: str,
        note: str,
        created_by_id: int | None,
    ) -> "MemberAlias":
        from nycti.db.models import MemberAlias

        normalized_alias = normalize_member_alias(alias)
        if normalized_alias is None:
            raise ValueError("Alias must be 1-40 chars using letters, numbers, spaces, `.`, `_`, or `-`.")
        cleaned_note = normalize_member_note(note)
        existing = await session.scalar(
            select(MemberAlias).where(
                MemberAlias.guild_id == guild_id,
                func.lower(MemberAlias.alias) == normalized_alias.lower(),
            )
        )
        if existing is not None:
            existing.user_id = user_id
            existing.alias = normalized_alias
            existing.note = cleaned_note
            existing.created_by_id = created_by_id
            await session.flush()
            return existing
        member_alias = MemberAlias(
            guild_id=guild_id,
            user_id=user_id,
            alias=normalized_alias,
            note=cleaned_note,
            created_by_id=created_by_id,
        )
        session.add(member_alias)
        await session.flush()
        return member_alias

    async def delete_alias(self, session: AsyncSession, *, guild_id: int, alias: str) -> bool:
        from nycti.db.models import MemberAlias

        cleaned = alias.strip()
        if not cleaned:
            return False
        if cleaned.isdigit():
            member_alias = await session.get(MemberAlias, int(cleaned))
            if member_alias is None or member_alias.guild_id != guild_id:
                return False
        else:
            normalized_alias = normalize_member_alias(cleaned)
            if normalized_alias is None:
                return False
            member_alias = await session.scalar(
                select(MemberAlias).where(
                    MemberAlias.guild_id == guild_id,
                    func.lower(MemberAlias.alias) == normalized_alias.lower(),
                )
            )
            if member_alias is None:
                return False
        await session.delete(member_alias)
        await session.flush()
        return True

    async def list_aliases(self, session: AsyncSession, *, guild_id: int) -> list["MemberAlias"]:
        from nycti.db.models import MemberAlias

        stmt = (
            select(MemberAlias)
            .where(MemberAlias.guild_id == guild_id)
            .order_by(MemberAlias.alias.asc(), MemberAlias.user_id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def list_matching_aliases(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        text: str,
    ) -> list["MemberAlias"]:
        aliases = await self.list_aliases(session, guild_id=guild_id)
        return [alias for alias in aliases if member_alias_matches(alias.alias, text)]


def normalize_member_alias(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if not cleaned or len(cleaned) > MAX_ALIAS_LENGTH:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", cleaned):
        return None
    return cleaned


def normalize_member_note(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned[:MAX_NOTE_LENGTH].rstrip()


def member_alias_matches(alias: str, text: str) -> bool:
    cleaned_alias = normalize_member_alias(alias)
    if cleaned_alias is None or not text.strip():
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(cleaned_alias) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def format_member_alias_list(aliases: list[object]) -> str:
    if not aliases:
        return "No member aliases configured for this server."
    lines = ["Configured member aliases:"]
    for item in aliases:
        note = f" - {item.note}" if getattr(item, "note", "") else ""
        lines.append(f"- `{item.id}` `{item.alias}` -> <@{item.user_id}>{note}")
    return "\n".join(lines)
