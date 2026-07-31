from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from nycti.memory.filtering import (
    has_durable_memory_signal,
    should_skip_memory_extraction,
)

LOGGER = logging.getLogger(__name__)
PROFILE_UPDATE_STATE_KEY_PREFIX = "profile_update_at"
DEFAULT_MEMORY_QUEUE_MAXSIZE = 64


@dataclass(frozen=True, slots=True)
class MemoryWriteJob:
    guild_id: int | None
    channel_id: int | None
    user_id: int
    source_message_id: int | None
    current_message: str
    recent_context: str


@dataclass(slots=True)
class _UserLockEntry:
    lock: asyncio.Lock
    references: int = 0


class BackgroundMemoryWriter:
    def __init__(
        self,
        *,
        settings: Any,
        database: Any,
        memory_service: Any,
        queue_maxsize: int = DEFAULT_MEMORY_QUEUE_MAXSIZE,
    ) -> None:
        self.settings = settings
        self.database = database
        self.memory_service = memory_service
        self._user_locks: dict[int, _UserLockEntry] = {}
        self._queue: asyncio.Queue[MemoryWriteJob] = asyncio.Queue(
            maxsize=max(1, queue_maxsize)
        )
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        task = self._worker_task
        if task is None or task.done():
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name="nycti-memory-writer",
            )
            LOGGER.info(
                "Started background memory queue worker model=%s capacity=%s.",
                getattr(self.settings, "openai_memory_model", "(configured service)"),
                self._queue.maxsize,
            )

    async def close(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def schedule(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> bool:
        if should_skip_memory_extraction(current_message)[0]:
            return False
        self.start()
        job = MemoryWriteJob(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            current_message=current_message,
            recent_context=recent_context,
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            LOGGER.warning(
                "Background memory queue is full; dropping optional memory work for message %s.",
                source_message_id,
            )
            return False
        return True

    async def _run_worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self.run(
                    guild_id=job.guild_id,
                    channel_id=job.channel_id,
                    user_id=job.user_id,
                    source_message_id=job.source_message_id,
                    current_message=job.current_message,
                    recent_context=job.recent_context,
                )
            finally:
                self._queue.task_done()

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
        skip, _reason = should_skip_memory_extraction(current_message)
        if skip:
            return
        user_lock = self._retain_user_lock(user_id)
        try:
            async with user_lock.lock:
                await self._run_serialized(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    source_message_id=source_message_id,
                    current_message=current_message,
                    recent_context=recent_context,
                )
        except Exception as exc:  # pragma: no cover - defensive background path
            from nycti.llm.client import is_transient_provider_error

            if is_transient_provider_error(exc):
                detail = " ".join(str(exc).split())[:240]
                LOGGER.warning("Memory extraction skipped after transient provider failure: %s", detail)
                return
            LOGGER.exception("Memory extraction failed.")
        finally:
            self._release_user_lock(user_id, user_lock)

    def _retain_user_lock(self, user_id: int) -> _UserLockEntry:
        entry = self._user_locks.get(user_id)
        if entry is None:
            entry = _UserLockEntry(lock=asyncio.Lock())
            self._user_locks[user_id] = entry
        entry.references += 1
        return entry

    def _release_user_lock(self, user_id: int, entry: _UserLockEntry) -> None:
        entry.references -= 1
        if entry.references == 0 and self._user_locks.get(user_id) is entry:
            self._user_locks.pop(user_id, None)

    async def _run_serialized(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
    ) -> None:
        from nycti.usage import record_usage

        async with self.database.session() as session:
            enabled = await self.memory_service.is_enabled(session, user_id)
            await session.commit()
        if not enabled:
            return

        generate_many = getattr(self.memory_service, "generate_memory_candidates", None)
        if callable(generate_many):
            candidates, memory_result = await generate_many(
                current_message=current_message,
                recent_context=recent_context,
            )
        else:
            candidate, memory_result = await self.memory_service.generate_memory_candidate(
                current_message=current_message,
                recent_context=recent_context,
            )
            candidates = [candidate] if candidate is not None else []
        now_utc = datetime.now(timezone.utc)
        embedding_targets: list[tuple[int, Any]] = []
        should_consider_profile = False
        force_profile_update = False
        should_consider_consolidation = False

        async with self.database.session() as session:
            if memory_result is not None:
                await record_usage(
                    session,
                    usage=memory_result.usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
            stored_memories = []
            for candidate in candidates:
                stored_memory = await self.memory_service.store_memory_candidate(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    source_message_id=source_message_id,
                    candidate=candidate,
                )
                if stored_memory is None:
                    continue
                stored_memories.append(stored_memory)
                if candidate.summary.strip() and stored_memory.embedding is None:
                    embedding_targets.append((int(stored_memory.id), candidate))
            ticker_interest_only = bool(candidates) and all(
                str(getattr(candidate, "predicate", "")).startswith(
                    ("stock_ticker_interest_", "shared_market_report_ticker_")
                )
                for candidate in candidates
            )
            should_consider_profile = bool(
                not ticker_interest_only
                and (stored_memories or has_durable_memory_signal(current_message))
            )
            force_profile_update = bool(stored_memories)
            should_consider_consolidation = bool(stored_memories)
            await session.commit()

        for stored_memory_id, candidate in embedding_targets:
            async with self.database.session() as session:
                embedding_target_is_current = (
                    await self.memory_service.memory_embedding_target_is_current(
                        session,
                        memory_id=stored_memory_id,
                        user_id=user_id,
                        expected_summary=candidate.summary,
                    )
                )
                await session.commit()
            embedding_result = (
                await self.memory_service.generate_memory_storage_embedding(candidate.summary)
                if embedding_target_is_current
                else None
            )
            if embedding_result is not None:
                async with self.database.session() as session:
                    await self.memory_service.attach_memory_embedding(
                        session,
                        memory_id=stored_memory_id,
                        user_id=user_id,
                        embedding=embedding_result.embedding,
                    )
                    await record_usage(
                        session,
                        usage=embedding_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    await session.commit()

        profile_snapshot: str | None = None
        if should_consider_profile:
            async with self.database.session() as session:
                profile_enabled = await self.memory_service.is_enabled(session, user_id)
                if profile_enabled and await self.should_run_profile_update(
                    session,
                    user_id=user_id,
                    now=now_utc,
                    force=force_profile_update,
                ):
                    profile_snapshot = await self.memory_service.get_personal_profile_md(
                        session,
                        user_id,
                    )
                await session.commit()
        if profile_snapshot is not None:
            profile_md, profile_result = (
                await self.memory_service.generate_personal_profile_update(
                    existing_profile=profile_snapshot,
                    current_message=current_message,
                    recent_context=recent_context,
                )
            )
            if profile_result is not None:
                async with self.database.session() as session:
                    await record_usage(
                        session,
                        usage=profile_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    profile_is_current = (
                        await self.memory_service.is_enabled(session, user_id)
                        and await self.memory_service.get_personal_profile_md(session, user_id)
                        == profile_snapshot
                    )
                    if profile_is_current:
                        if profile_md is not None:
                            await self.memory_service.apply_personal_profile_update(
                                session,
                                user_id=user_id,
                                profile_md=profile_md,
                                expected_profile=profile_snapshot,
                            )
                        await self.touch_profile_update_state(
                            session,
                            user_id=user_id,
                            when=now_utc,
                        )
                    await session.commit()

        consolidation_plan: Any | None = None
        if should_consider_consolidation:
            async with self.database.session() as session:
                consolidation_plan = await self.memory_service.prepare_memory_consolidation(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    now=datetime.now(timezone.utc),
                )
                await session.commit()
        if consolidation_plan is not None:
            decision, consolidation_result = (
                await self.memory_service.generate_memory_consolidation(consolidation_plan)
            )
            if consolidation_result is not None:
                async with self.database.session() as session:
                    await self.memory_service.apply_memory_consolidation(
                        session,
                        plan=consolidation_plan,
                        decision=decision,
                    )
                    await record_usage(
                        session,
                        usage=consolidation_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    await session.commit()

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
