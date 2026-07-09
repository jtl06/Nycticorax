from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from nycti.memory.filtering import has_useful_memory_signal

LOGGER = logging.getLogger(__name__)
PROFILE_UPDATE_STATE_KEY_PREFIX = "profile_update_at"


class BackgroundMemoryWriter:
    def __init__(
        self,
        *,
        settings: Any,
        database: Any,
        memory_service: Any,
    ) -> None:
        self.settings = settings
        self.database = database
        self.memory_service = memory_service

    def schedule(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> None:
        asyncio.create_task(
            self.run(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                current_message=current_message,
                recent_context=recent_context,
            )
        )

    async def run(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> None:
        try:
            async with self.database.session() as session:
                now_utc = datetime.now(timezone.utc)
                caller_has_durable_signal = has_useful_memory_signal(current_message)

                for target_user_id in (user_id,):
                    stored_memory, memory_result = await self.memory_service.maybe_store_memory(
                        session,
                        user_id=target_user_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        source_message_id=source_message_id,
                        current_message=current_message,
                        recent_context=recent_context,
                    )
                    if memory_result is not None:
                        from nycti.usage import record_usage

                        await record_usage(
                            session,
                            usage=memory_result.usage,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=target_user_id,
                        )
                    should_update_profile = stored_memory is not None or (
                        caller_has_durable_signal and target_user_id == user_id
                    )
                    if not should_update_profile:
                        continue
                    if not await self.should_run_profile_update(
                        session,
                        user_id=target_user_id,
                        now=now_utc,
                        force=stored_memory is not None,
                    ):
                        continue
                    profile_result = await self.memory_service.maybe_update_personal_profile(
                        session,
                        user_id=target_user_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        current_message=current_message,
                        recent_context=recent_context,
                    )
                    if profile_result is not None:
                        from nycti.usage import record_usage

                        await record_usage(
                            session,
                            usage=profile_result.usage,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=target_user_id,
                        )
                        await self.touch_profile_update_state(
                            session,
                            user_id=target_user_id,
                            when=now_utc,
                        )
                await session.commit()
        except Exception as exc:  # pragma: no cover - defensive background path
            from nycti.llm.client import is_transient_provider_error

            if is_transient_provider_error(exc):
                detail = " ".join(str(exc).split())[:240]
                LOGGER.warning("Memory extraction skipped after transient provider failure: %s", detail)
                return
            LOGGER.exception("Memory extraction failed.")

    async def should_run_profile_update(
        self,
        session: Any,
        *,
        user_id: int,
        now: datetime,
        force: bool,
    ) -> bool:
        cooldown_seconds = self.settings.profile_update_cooldown_seconds
        if force or cooldown_seconds <= 0:
            return True
        from nycti.db.models import AppState

        state = await session.get(AppState, self.profile_update_state_key(user_id))
        if state is None:
            return True
        try:
            last_updated = datetime.fromisoformat(state.value)
        except ValueError:
            return True
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now - last_updated.astimezone(timezone.utc)).total_seconds()
        return elapsed_seconds >= cooldown_seconds

    async def touch_profile_update_state(
        self,
        session: Any,
        *,
        user_id: int,
        when: datetime,
    ) -> None:
        from nycti.db.models import AppState

        key = self.profile_update_state_key(user_id)
        state = await session.get(AppState, key)
        value = when.astimezone(timezone.utc).isoformat()
        if state is None:
            session.add(AppState(key=key, value=value))
            await session.flush()
            return
        state.value = value
        await session.flush()

    @staticmethod
    def profile_update_state_key(user_id: int) -> str:
        return f"{PROFILE_UPDATE_STATE_KEY_PREFIX}:{user_id}"
