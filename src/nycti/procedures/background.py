from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Iterable, Mapping

from nycti.procedures.service import (
    ProcedureMemoryService,
    parse_procedure_ids,
    parse_tool_names,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_PROCEDURE_QUEUE_MAXSIZE = 32
NON_LEARNABLE_TOOLS = frozenset({"reminder", "send_msg", "profile_update"})
DIRECT_SINGLE_STEP_TOOLS = frozenset(
    {
        "annual_perf",
        "calc",
        "channel_ctx",
        "img_search",
        "memory_search",
        "price_hist",
        "quote",
        "url_extract",
        "yt_transcript",
    }
)


@dataclass(frozen=True, slots=True)
class ProcedureLearningJob:
    guild_id: int
    channel_id: int | None
    user_id: int | None
    source_message_id: int | None
    source_run_id: str | None
    request_text: str
    successful_tools: tuple[str, ...]
    selected_procedure_ids: tuple[int, ...]


class BackgroundProcedureLearner:
    def __init__(
        self,
        *,
        database,
        service: ProcedureMemoryService,
        queue_maxsize: int = DEFAULT_PROCEDURE_QUEUE_MAXSIZE,
    ) -> None:
        self.database = database
        self.service = service
        self._queue: asyncio.Queue[ProcedureLearningJob] = asyncio.Queue(
            maxsize=max(1, queue_maxsize)
        )
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name="nycti-procedure-learner",
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
        user_id: int | None,
        source_message_id: int | None,
        source_run_id: str | None,
        request_text: str,
        successful_tools: Iterable[str],
        selected_procedure_ids: Iterable[int] = (),
    ) -> bool:
        if guild_id is None or not request_text.strip():
            return False
        tools = tuple(
            name
            for name in dict.fromkeys(
                str(value).strip().casefold() for value in successful_tools
            )
            if name and name not in NON_LEARNABLE_TOOLS
        )
        if not tools:
            return False
        if len(tools) == 1 and tools[0] in DIRECT_SINGLE_STEP_TOOLS:
            return False
        ids = tuple(
            dict.fromkeys(
                int(value) for value in selected_procedure_ids if int(value) > 0
            )
        )
        self.start()
        job = ProcedureLearningJob(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            source_run_id=source_run_id,
            request_text=request_text,
            successful_tools=tools,
            selected_procedure_ids=ids,
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            LOGGER.warning(
                "Procedure-learning queue is full; dropping optional run %s.",
                source_run_id,
            )
            return False
        return True

    def schedule_from_metrics(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        source_message_id: int | None,
        request_text: str,
        metrics: Mapping[str, int | str] | None,
    ) -> bool:
        if metrics is None:
            return False
        if (
            str(metrics.get("agent_final_status", "")).casefold() != "success"
            or str(metrics.get("agent_stop_reason", "")).casefold() != "final_text"
            or int(metrics.get("agent_correction_count", 0) or 0) > 0
        ):
            return False
        return self.schedule(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            source_run_id=str(metrics.get("agent_run_id", "") or "") or None,
            request_text=request_text,
            successful_tools=parse_tool_names(
                metrics.get(
                    "agent_successful_tool_sequence",
                    metrics.get("routing_successful_tools", ""),
                )
            ),
            selected_procedure_ids=parse_procedure_ids(
                metrics.get("procedure_memory_ids", "")
            ),
        )

    async def _run_worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self.run(job)
            except Exception:  # pragma: no cover - defensive background path
                LOGGER.exception("Procedure learning failed for run %s.", job.source_run_id)
            finally:
                self._queue.task_done()

    async def run(self, job: ProcedureLearningJob) -> None:
        from nycti.usage import record_usage

        async with self.database.session() as session:
            reinforced = await self.service.reinforce_selected(
                session,
                procedure_ids=job.selected_procedure_ids,
                successful_tools=job.successful_tools,
            )
            if reinforced is None:
                reinforced = await self.service.find_reinforcement_match(
                    session,
                    guild_id=job.guild_id,
                    request_text=job.request_text,
                    successful_tools=job.successful_tools,
                )
                if reinforced is not None:
                    self.service.reinforce(reinforced)
            if reinforced is not None:
                await session.commit()
                self.service.invalidate(job.guild_id)
                LOGGER.info(
                    "Procedure memory reinforced id=%s status=%s successes=%s run=%s.",
                    reinforced.id,
                    reinforced.status,
                    reinforced.success_count,
                    job.source_run_id,
                )
                return

        candidate, result = await self.service.generate_candidate(
            request_text=job.request_text,
            successful_tools=job.successful_tools,
        )
        if result is None:
            return
        async with self.database.session() as session:
            await record_usage(
                session,
                usage=result.usage,
                guild_id=job.guild_id,
                channel_id=job.channel_id,
                user_id=job.user_id,
            )
            if candidate is not None:
                stored = await self.service.store_candidate(
                    session,
                    guild_id=job.guild_id,
                    source_message_id=job.source_message_id,
                    source_run_id=job.source_run_id,
                    candidate=candidate,
                )
                LOGGER.info(
                    "Procedure memory learned id=%s status=%s successes=%s run=%s.",
                    stored.id,
                    stored.status,
                    stored.success_count,
                    job.source_run_id,
                )
            await session.commit()
            self.service.invalidate(job.guild_id)
