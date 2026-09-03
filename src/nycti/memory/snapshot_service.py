from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Memory, MemorySnapshot, UserSettings
from nycti.memory.extractor import TICKER_SYMBOL_RE
from nycti.memory.lifecycle import ACTIVE_MEMORY_STATUS
from nycti.memory.snapshots import (
    GUILD_SNAPSHOT_SCOPE,
    MAX_GUILD_SNAPSHOT_ITEMS,
    MAX_SNAPSHOT_CANDIDATES,
    MAX_USER_SNAPSHOT_ITEMS,
    MemorySnapshotBuild,
    USER_SNAPSHOT_SCOPE,
    build_memory_snapshot,
    memory_snapshot_scope_key,
)
from nycti.memory.visibility import MemoryVisibility


MAX_ACTIVE_MARKET_WATCHLIST_SYMBOLS = 24


@dataclass(frozen=True, slots=True)
class MemorySnapshotBlocks:
    user: str = ""
    guild: str = ""
    source_count: int = 0

    @property
    def rendered(self) -> str:
        sections: list[str] = []
        if self.user.strip():
            sections.append(f"User memory:\n{self.user.strip()}")
        if self.guild.strip():
            sections.append(f"Server memory:\n{self.guild.strip()}")
        return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ActiveMarketWatchlist:
    """Typed market preferences that must not compete with prose snapshots."""

    personal: tuple[str, ...] = ()
    shared: tuple[str, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.personal, *self.shared)))


class MemorySnapshotMixin:
    async def get_memory_snapshot_blocks(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        memory_enabled: bool | None = None,
    ) -> MemorySnapshotBlocks:
        enabled = (
            await self.is_enabled(session, user_id)
            if memory_enabled is None
            else memory_enabled
        )
        if not enabled:
            return MemorySnapshotBlocks()
        scoped_user_key = memory_snapshot_scope_key(
            USER_SNAPSHOT_SCOPE,
            user_id=user_id,
            guild_id=guild_id,
        )
        global_user_key = memory_snapshot_scope_key(
            USER_SNAPSHOT_SCOPE,
            user_id=user_id,
            guild_id=None,
        )
        guild_key = (
            memory_snapshot_scope_key(GUILD_SNAPSHOT_SCOPE, guild_id=guild_id)
            if guild_id is not None
            else None
        )
        snapshot_keys = tuple(
            dict.fromkeys(
                key
                for key in (scoped_user_key, global_user_key, guild_key)
                if key is not None
            )
        )
        snapshots = {
            snapshot.scope_key: snapshot
            for snapshot in (
                await session.scalars(
                    select(MemorySnapshot).where(
                        MemorySnapshot.scope_key.in_(snapshot_keys)
                    )
                )
            ).all()
        }
        user_snapshot = snapshots.get(scoped_user_key)
        if user_snapshot is None and guild_id is not None:
            user_snapshot = snapshots.get(global_user_key)
        guild_snapshot = snapshots.get(guild_key) if guild_key is not None else None
        return MemorySnapshotBlocks(
            user=user_snapshot.content_md if user_snapshot is not None else "",
            guild=guild_snapshot.content_md if guild_snapshot is not None else "",
            source_count=(
                (user_snapshot.item_count if user_snapshot is not None else 0)
                + (guild_snapshot.item_count if guild_snapshot is not None else 0)
            ),
        )

    async def get_active_market_watchlist(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime | None = None,
        memory_enabled: bool | None = None,
    ) -> ActiveMarketWatchlist:
        enabled = (
            await self.is_enabled(session, user_id)
            if memory_enabled is None
            else memory_enabled
        )
        if not enabled:
            return ActiveMarketWatchlist()
        current_time = now or datetime.now(timezone.utc)
        visibility_filter = and_(
            Memory.user_id == user_id,
            Memory.visibility == MemoryVisibility.PRIVATE.value,
            Memory.predicate.like("stock_ticker_interest_%"),
        )
        if guild_id is not None:
            visibility_filter = or_(
                visibility_filter,
                and_(
                    Memory.guild_id == guild_id,
                    Memory.visibility == MemoryVisibility.GUILD_SHARED.value,
                    Memory.predicate.like("shared_market_report_ticker_%"),
                ),
            )
        rows = (
            await session.execute(
                select(
                    Memory.user_id,
                    Memory.visibility,
                    Memory.object_text,
                )
                .join(UserSettings, UserSettings.user_id == Memory.user_id)
                .where(
                    UserSettings.memory_enabled.is_(True),
                    Memory.status == ACTIVE_MEMORY_STATUS,
                    or_(Memory.expires_at.is_(None), Memory.expires_at > current_time),
                    or_(Memory.valid_until.is_(None), Memory.valid_until > current_time),
                    visibility_filter,
                )
                .order_by(desc(Memory.last_confirmed_at), desc(Memory.updated_at))
                .limit(MAX_ACTIVE_MARKET_WATCHLIST_SYMBOLS)
            )
        ).all()
        personal: list[str] = []
        shared: list[str] = []
        for owner_user_id, visibility, object_text in rows:
            symbol = str(object_text or "").strip().removeprefix("$").upper()
            if not TICKER_SYMBOL_RE.fullmatch(symbol):
                continue
            target = (
                personal
                if int(owner_user_id) == user_id
                and visibility == MemoryVisibility.PRIVATE.value
                else shared
            )
            if symbol not in target:
                target.append(symbol)
        return ActiveMarketWatchlist(tuple(personal), tuple(shared))

    async def rebuild_memory_snapshots(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime | None = None,
    ) -> MemorySnapshotBlocks:
        current_time = now or datetime.now(timezone.utc)
        if await self.is_enabled(session, user_id):
            await self._rebuild_user_memory_snapshot(
                session,
                user_id=user_id,
                guild_id=guild_id,
                now=current_time,
            )
        else:
            await session.execute(
                delete(MemorySnapshot).where(MemorySnapshot.user_id == user_id)
            )
        if guild_id is not None:
            await self._rebuild_guild_memory_snapshot(
                session,
                guild_id=guild_id,
                now=current_time,
            )
        await session.flush()
        return await self.get_memory_snapshot_blocks(
            session,
            user_id=user_id,
            guild_id=guild_id,
        )

    async def refresh_all_memory_snapshots(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> int:
        current_time = now or datetime.now(timezone.utc)
        enabled_users = set(await self.get_enabled_user_ids(session, user_ids=None))
        user_scopes = {
            (int(user_id), int(guild_id) if guild_id is not None else None)
            for user_id, guild_id in (
                await session.execute(
                    select(Memory.user_id, Memory.guild_id)
                    .where(
                        Memory.user_id.in_(enabled_users) if enabled_users else False,
                        Memory.visibility == MemoryVisibility.PRIVATE.value,
                        Memory.status == ACTIVE_MEMORY_STATUS,
                    )
                    .distinct()
                )
            ).all()
        }
        guild_ids = {
            int(guild_id)
            for guild_id in (
                await session.scalars(
                    select(Memory.guild_id)
                    .join(UserSettings, UserSettings.user_id == Memory.user_id)
                    .where(
                        UserSettings.memory_enabled.is_(True),
                        Memory.guild_id.is_not(None),
                        Memory.visibility.in_(
                            [
                                MemoryVisibility.GUILD_SHARED.value,
                                MemoryVisibility.LORE.value,
                            ]
                        ),
                        Memory.status == ACTIVE_MEMORY_STATUS,
                    )
                    .distinct()
                )
            ).all()
            if guild_id is not None
        }
        existing = list((await session.scalars(select(MemorySnapshot))).all())
        for snapshot in existing:
            if snapshot.scope_type == USER_SNAPSHOT_SCOPE and snapshot.user_id is not None:
                user_scopes.add((int(snapshot.user_id), snapshot.guild_id))
            elif snapshot.scope_type == GUILD_SNAPSHOT_SCOPE and snapshot.guild_id is not None:
                guild_ids.add(int(snapshot.guild_id))
        for scoped_user_id, scoped_guild_id in sorted(
            user_scopes,
            key=lambda item: (item[0], item[1] or 0),
        ):
            if scoped_user_id in enabled_users:
                await self._rebuild_user_memory_snapshot(
                    session,
                    user_id=scoped_user_id,
                    guild_id=scoped_guild_id,
                    now=current_time,
                )
            else:
                await session.execute(
                    delete(MemorySnapshot).where(MemorySnapshot.user_id == scoped_user_id)
                )
        for scoped_guild_id in sorted(guild_ids):
            await self._rebuild_guild_memory_snapshot(
                session,
                guild_id=scoped_guild_id,
                now=current_time,
            )
        await session.flush()
        return len(user_scopes) + len(guild_ids)

    async def _rebuild_user_memory_snapshot(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime,
    ) -> None:
        guild_scope = (
            Memory.guild_id.is_(None)
            if guild_id is None
            else or_(Memory.guild_id.is_(None), Memory.guild_id == guild_id)
        )
        memories = list(
            (
                await session.scalars(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        guild_scope,
                        Memory.visibility == MemoryVisibility.PRIVATE.value,
                        Memory.status == ACTIVE_MEMORY_STATUS,
                    )
                    .order_by(desc(Memory.updated_at), desc(Memory.created_at))
                    .limit(MAX_SNAPSHOT_CANDIDATES)
                )
            ).all()
        )
        built = build_memory_snapshot(
            memories,
            scope_type=USER_SNAPSHOT_SCOPE,
            max_chars=getattr(
                getattr(self.extractor, "settings", None),
                "memory_user_snapshot_max_chars",
                2400,
            ),
            max_items=MAX_USER_SNAPSHOT_ITEMS,
            now=now,
        )
        await self._replace_memory_snapshot(
            session,
            scope_key=memory_snapshot_scope_key(
                USER_SNAPSHOT_SCOPE,
                user_id=user_id,
                guild_id=guild_id,
            ),
            scope_type=USER_SNAPSHOT_SCOPE,
            user_id=user_id,
            guild_id=guild_id,
            built=built,
            now=now,
        )

    async def _rebuild_guild_memory_snapshot(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        now: datetime,
    ) -> None:
        memories = list(
            (
                await session.scalars(
                    select(Memory)
                    .join(UserSettings, UserSettings.user_id == Memory.user_id)
                    .where(
                        UserSettings.memory_enabled.is_(True),
                        Memory.guild_id == guild_id,
                        Memory.visibility.in_(
                            [
                                MemoryVisibility.GUILD_SHARED.value,
                                MemoryVisibility.LORE.value,
                            ]
                        ),
                        Memory.status == ACTIVE_MEMORY_STATUS,
                    )
                    .order_by(desc(Memory.updated_at), desc(Memory.created_at))
                    .limit(MAX_SNAPSHOT_CANDIDATES)
                )
            ).all()
        )
        built = build_memory_snapshot(
            memories,
            scope_type=GUILD_SNAPSHOT_SCOPE,
            max_chars=getattr(
                getattr(self.extractor, "settings", None),
                "memory_guild_snapshot_max_chars",
                2200,
            ),
            max_items=MAX_GUILD_SNAPSHOT_ITEMS,
            now=now,
        )
        await self._replace_memory_snapshot(
            session,
            scope_key=memory_snapshot_scope_key(
                GUILD_SNAPSHOT_SCOPE,
                guild_id=guild_id,
            ),
            scope_type=GUILD_SNAPSHOT_SCOPE,
            user_id=None,
            guild_id=guild_id,
            built=built,
            now=now,
        )

    @staticmethod
    async def _replace_memory_snapshot(
        session: AsyncSession,
        *,
        scope_key: str,
        scope_type: str,
        user_id: int | None,
        guild_id: int | None,
        built: MemorySnapshotBuild,
        now: datetime,
    ) -> None:
        existing = await session.get(MemorySnapshot, scope_key)
        content_md = built.content_md
        if not content_md:
            if existing is not None:
                await session.delete(existing)
            return
        fingerprint = built.fingerprint
        if existing is not None and existing.source_fingerprint == fingerprint:
            return
        source_ids = list(built.source_memory_ids)
        if existing is None:
            session.add(
                MemorySnapshot(
                    scope_key=scope_key,
                    scope_type=scope_type,
                    user_id=user_id,
                    guild_id=guild_id,
                    content_md=content_md,
                    source_memory_ids=source_ids,
                    item_count=len(source_ids),
                    source_fingerprint=fingerprint,
                    created_at=now,
                    updated_at=now,
                )
            )
            return
        existing.content_md = content_md
        existing.source_memory_ids = source_ids
        existing.item_count = len(source_ids)
        existing.source_fingerprint = fingerprint
        existing.updated_at = now
