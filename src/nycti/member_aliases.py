from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from nycti.db.models import MemberAlias


MAX_ALIAS_LENGTH = 40
MAX_NOTE_LENGTH = 160
MAX_MEMBER_NAME_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ObservedMemberIdentity:
    user_id: int
    username: str
    global_name: str
    display_name: str


class MemberAliasService:
    def __init__(self) -> None:
        self._observed_identity_signatures: dict[
            tuple[int, int],
            tuple[str, str, str],
        ] = {}

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

    def needs_identity_update(self, *, guild_id: int, member: object) -> bool:
        observed = observed_member_identity(member)
        if observed is None:
            return False
        signature = (
            observed.username,
            observed.global_name,
            observed.display_name,
        )
        return self._observed_identity_signatures.get(
            (guild_id, observed.user_id)
        ) != signature

    def forget_observed_members(
        self,
        *,
        guild_id: int,
        members: Iterable[object],
    ) -> None:
        for member in members:
            observed = observed_member_identity(member)
            if observed is not None:
                self._observed_identity_signatures.pop(
                    (guild_id, observed.user_id),
                    None,
                )

    async def remember_observed_members(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        members: Iterable[object],
    ) -> int:
        from nycti.db.models import MemberIdentity

        observations: dict[int, ObservedMemberIdentity] = {}
        for member in members:
            observed = observed_member_identity(member)
            if observed is None or observed.user_id in observations:
                continue
            observations[observed.user_id] = observed
        pending = {
            user_id: observed
            for user_id, observed in observations.items()
            if self._observed_identity_signatures.get((guild_id, user_id))
            != (
                observed.username,
                observed.global_name,
                observed.display_name,
            )
        }
        if not pending:
            return 0
        existing_by_user_id = {
            identity.user_id: identity
            for identity in (
                await session.scalars(
                    select(MemberIdentity).where(
                        MemberIdentity.guild_id == guild_id,
                        MemberIdentity.user_id.in_(pending),
                    )
                )
            ).all()
        }
        for observed in pending.values():
            signature = (
                observed.username,
                observed.global_name,
                observed.display_name,
            )
            cache_key = (guild_id, observed.user_id)
            identity = existing_by_user_id.get(observed.user_id)
            if identity is None:
                session.add(
                    MemberIdentity(
                        guild_id=guild_id,
                        user_id=observed.user_id,
                        username=observed.username,
                        global_name=observed.global_name,
                        display_name=observed.display_name,
                    )
                )
            else:
                identity.username = observed.username
                identity.global_name = observed.global_name
                identity.display_name = observed.display_name
            self._observed_identity_signatures[cache_key] = signature
        await session.flush()
        return len(pending)

    async def list_matching_identities(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        text: str,
    ) -> list[object]:
        from nycti.db.models import MemberIdentity

        identities = list(
            (
                await session.scalars(
                    select(MemberIdentity)
                    .where(MemberIdentity.guild_id == guild_id)
                    .order_by(MemberIdentity.updated_at.desc(), MemberIdentity.user_id.asc())
                )
            ).all()
        )
        return [
            identity
            for identity in identities
            if any(
                member_alias_matches(name, text)
                for name in member_identity_names(identity)
            )
        ]


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


def observed_member_identity(member: object) -> ObservedMemberIdentity | None:
    user_id = getattr(member, "id", None)
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or bool(getattr(member, "bot", False))
    ):
        return None
    return ObservedMemberIdentity(
        user_id=user_id,
        username=_clean_member_name(getattr(member, "name", "")),
        global_name=_clean_member_name(getattr(member, "global_name", "")),
        display_name=_clean_member_name(getattr(member, "display_name", "")),
    )


def member_identity_names(identity: object) -> tuple[str, ...]:
    names: list[str] = []
    for attribute in ("display_name", "global_name", "username"):
        cleaned = _clean_member_name(getattr(identity, attribute, ""))
        if cleaned and cleaned.casefold() not in {name.casefold() for name in names}:
            names.append(cleaned)
    return tuple(names)


def format_member_reference_block(
    aliases: list[object],
    identities: list[object],
) -> str:
    lines: list[str] = []
    seen: set[tuple[int, str]] = set()
    for identity in identities:
        names = member_identity_names(identity)
        if not names:
            continue
        user_id = int(identity.user_id)
        label = names[0]
        key = (user_id, label.casefold())
        if key in seen:
            continue
        seen.add(key)
        alternate_names = ", ".join(names[1:])
        suffix = f"; also {alternate_names}" if alternate_names else ""
        lines.append(
            f"- {label}: <@{user_id}> (user_id={user_id}{suffix})"
        )
    for alias in aliases:
        user_id = int(alias.user_id)
        key = (user_id, str(alias.alias).casefold())
        if key in seen:
            continue
        seen.add(key)
        note = f"; {alias.note}" if getattr(alias, "note", "") else ""
        lines.append(
            f"- {alias.alias}: <@{user_id}> (user_id={user_id}; server alias{note})"
        )
    return "\n".join(lines) if lines else "(none matched)"


def _clean_member_name(value: object) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned[:MAX_MEMBER_NAME_LENGTH].rstrip()


def format_member_alias_list(aliases: list[object]) -> str:
    if not aliases:
        return "No member aliases configured for this server."
    lines = ["Configured member aliases:"]
    for item in aliases:
        note = f" - {item.note}" if getattr(item, "note", "") else ""
        lines.append(f"- `{item.id}` `{item.alias}` -> <@{item.user_id}>{note}")
    return "\n".join(lines)
