from __future__ import annotations

import logging
from typing import Any

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - minimal test environments may omit discord.py
    class _DiscordStub:
        class Forbidden(Exception):
            pass

        class HTTPException(Exception):
            pass

        class NotFound(Exception):
            pass

    discord = _DiscordStub()
from sqlalchemy import select

from nycti.changelog import build_changelog_announcement

LOGGER = logging.getLogger(__name__)


class ChangelogService:
    def __init__(
        self,
        *,
        bot: Any,
        database: Any,
        settings: Any,
        state_model: Any = None,
        select_factory: Any = select,
    ) -> None:
        self.bot = bot
        self.database = database
        self.settings = settings
        self._state_model_override = state_model
        self._select = select_factory

    async def post_startup(self) -> None:
        await self.bot.wait_until_ready()
        async with self.database.session() as session:
            channel_ids = await self.list_configured_channels(session)
            if not channel_ids:
                return
            posted_any = False
            for guild_id, channel_id in channel_ids:
                previous_snapshot = await self.get_last_snapshot(session, guild_id=guild_id)
                announcement = build_changelog_announcement(
                    self.settings,
                    previous_snapshot=previous_snapshot,
                )
                if announcement is None:
                    continue
                if not await self.post_announcement(channel_id, announcement.content):
                    continue
                await self.set_last_snapshot(
                    session,
                    guild_id=guild_id,
                    snapshot=announcement.snapshot,
                )
                posted_any = True
            if posted_any:
                await session.commit()

    async def post_announcement(self, channel_id: int, content: str) -> bool:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                LOGGER.warning("Failed to fetch changelog channel %s.", channel_id)
                return False
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed to post changelog into channel %s.", channel_id)
            return False
        return True

    async def get_last_snapshot(self, session: Any, *, guild_id: int) -> str | None:
        state = await session.get(self._state_model(), self.snapshot_key(guild_id))
        return state.value if state is not None else None

    async def set_last_snapshot(
        self,
        session: Any,
        *,
        guild_id: int,
        snapshot: str,
    ) -> None:
        state_model = self._state_model()
        key = self.snapshot_key(guild_id)
        state = await session.get(state_model, key)
        if state is None:
            session.add(state_model(key=key, value=snapshot))
        else:
            state.value = snapshot
        await session.flush()

    async def list_configured_channels(self, session: Any) -> list[tuple[int, int]]:
        state_model = self._state_model()
        stmt = self._select(state_model).where(state_model.key.like("changelog_channel_id:%"))
        states = list((await session.scalars(stmt)).all())
        configured: list[tuple[int, int]] = []
        for state in states:
            try:
                guild_id = int(state.key.split(":", 1)[1])
                channel_id = int(state.value)
            except (IndexError, ValueError):
                continue
            configured.append((guild_id, channel_id))
        return configured

    async def get_channel_id(self, session: Any, *, guild_id: int) -> int | None:
        state = await session.get(self._state_model(), self.channel_key(guild_id))
        if state is None:
            return None
        try:
            return int(state.value)
        except ValueError:
            return None

    async def set_channel_id(
        self,
        session: Any,
        *,
        guild_id: int,
        channel_id: int | None,
    ) -> None:
        state_model = self._state_model()
        key = self.channel_key(guild_id)
        state = await session.get(state_model, key)
        if channel_id is None:
            if state is not None:
                await session.delete(state)
                await session.flush()
            return
        if state is None:
            session.add(state_model(key=key, value=str(channel_id)))
        else:
            state.value = str(channel_id)
        await session.flush()

    @staticmethod
    def channel_key(guild_id: int) -> str:
        return f"changelog_channel_id:{guild_id}"

    @staticmethod
    def snapshot_key(guild_id: int) -> str:
        return f"last_changelog_snapshot:{guild_id}"

    def _state_model(self) -> Any:
        if self._state_model_override is not None:
            return self._state_model_override
        from nycti.db.models import AppState

        return AppState
