from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from nycti.background_worker import BoundedBackgroundWorker
from nycti.memory.filtering import (
    should_skip_memory_extraction,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MEMORY_QUEUE_MAXSIZE = 64


@dataclass(frozen=True, slots=True)
class MemoryWriteJob:
    guild_id: int | None
    channel_id: int | None
    user_id: int
    source_message_id: int | None
    current_message: str
    recent_context: str
    correction_context: bool = False


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
        self._jobs = BoundedBackgroundWorker[MemoryWriteJob](
            handler=self._run_job,
            name="nycti-memory-writer",
            maxsize=queue_maxsize,
            logger=LOGGER,
            error_label="Background memory write",
        )

    @property
    def pending_count(self) -> int:
        return self._jobs.pending_count

    def start(self) -> None:
        if self._jobs.start():
            LOGGER.info(
                "Started background memory queue worker model=%s capacity=%s.",
                getattr(self.settings, "openai_memory_model", "(configured service)"),
                self._jobs.queue.maxsize,
            )

    async def close(self) -> None:
        await self._jobs.close()

    async def join(self) -> None:
        await self._jobs.join()

    def schedule(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        current_message: str,
        recent_context: str,
        correction_context: bool = False,
    ) -> bool:
        if should_skip_memory_extraction(
            current_message,
            correction_context=correction_context,
        )[0]:
            return False
        job = MemoryWriteJob(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            current_message=current_message,
            recent_context=recent_context,
            correction_context=correction_context,
        )
        if not self._jobs.submit(job):
            LOGGER.warning(
                "Background memory queue is full; dropping optional memory work for message %s.",
                source_message_id,
            )
            return False
        return True

    async def _run_job(self, job: MemoryWriteJob) -> None:
        await self.run(
            guild_id=job.guild_id,
            channel_id=job.channel_id,
            user_id=job.user_id,
            source_message_id=job.source_message_id,
            current_message=job.current_message,
            recent_context=job.recent_context,
            correction_context=job.correction_context,
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
        correction_context: bool = False,
    ) -> None:
        skip, _reason = should_skip_memory_extraction(
            current_message,
            correction_context=correction_context,
        )
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
                    correction_context=correction_context,
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
        correction_context: bool,
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
                correction_context=correction_context,
            )
        else:
            candidate, memory_result = await self.memory_service.generate_memory_candidate(
                current_message=current_message,
                recent_context=recent_context,
                correction_context=correction_context,
            )
            candidates = [candidate] if candidate is not None else []
        now_utc = datetime.now(timezone.utc)
        embedding_targets: list[tuple[int, Any]] = []
        stored_memories: list[Any] = []
        embedding_stored_count = 0
        consolidation_applied = False
        snapshots_refreshed = False
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
                    attached = await self.memory_service.attach_memory_embedding(
                        session,
                        memory_id=stored_memory_id,
                        user_id=user_id,
                        embedding=embedding_result.embedding,
                    )
                    embedding_stored_count += int(bool(attached))
                    await record_usage(
                        session,
                        usage=embedding_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
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
                    consolidation_applied = True
                    await record_usage(
                        session,
                        usage=consolidation_result.usage,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    await session.commit()

        if should_consider_consolidation:
            async with self.database.session() as session:
                await self.memory_service.rebuild_memory_snapshots(
                    session,
                    user_id=user_id,
                    guild_id=guild_id,
                    now=datetime.now(timezone.utc),
                )
                snapshots_refreshed = True
                await session.commit()

        LOGGER.info(
            "Memory outcome message=%s correction=%s candidates=%s stored=%s embedded=%s "
            "consolidation_applied=%s snapshots_refreshed=%s.",
            source_message_id,
            correction_context,
            len(candidates),
            len(stored_memories),
            embedding_stored_count,
            consolidation_applied,
            snapshots_refreshed,
        )
