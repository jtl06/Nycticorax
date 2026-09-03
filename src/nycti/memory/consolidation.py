from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import AppState, Memory
from nycti.formatting import parse_json_object_payload
from nycti.llm.types import LLMResult
from nycti.memory.extractor import coerce_json_bool
from nycti.memory.filtering import (
    contains_sensitive_pattern,
    contains_transient_memory_pattern,
)
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    MemoryStatus,
    normalize_related_entities,
)
from nycti.memory.visibility import MemoryVisibility


LOGGER = logging.getLogger(__name__)
MEMORY_CONSOLIDATION_STATE_PREFIX = "memory_consolidated_at"
MAX_CONSOLIDATION_SOURCES = 24
MAX_CONSOLIDATED_SUMMARY_CHARS = 480


@dataclass(frozen=True, slots=True)
class MemoryConsolidationSource:
    id: int
    memory_kind: str
    predicate: str | None
    object_text: str | None
    summary: str


@dataclass(frozen=True, slots=True)
class MemoryConsolidationPlan:
    user_id: int
    guild_id: int | None
    current_time: datetime
    state_key: str
    sources: tuple[MemoryConsolidationSource, ...]


@dataclass(frozen=True, slots=True)
class MemoryConsolidationDecision:
    summary: str
    source_ids: tuple[int, ...]
    related_entities: tuple[str, ...]


class MemoryConsolidationMixin:
    async def maybe_consolidate_memories(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime | None = None,
    ) -> tuple[Memory | None, LLMResult | None]:
        plan = await self.prepare_memory_consolidation(
            session,
            user_id=user_id,
            guild_id=guild_id,
            now=now,
        )
        if plan is None:
            return None, None
        decision, result = await self.generate_memory_consolidation(plan)
        if result is None:
            return None, None
        memory = await self.apply_memory_consolidation(
            session,
            plan=plan,
            decision=decision,
        )
        return memory, result

    async def prepare_memory_consolidation(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        guild_id: int | None,
        now: datetime | None = None,
    ) -> MemoryConsolidationPlan | None:
        if not await self.is_enabled(session, user_id):
            return None
        current_time = now or datetime.now(timezone.utc)
        state_key = f"{MEMORY_CONSOLIDATION_STATE_PREFIX}:{user_id}"
        if await self._consolidation_is_on_cooldown(
            session,
            state_key=state_key,
            now=current_time,
            cooldown_seconds=getattr(
                self.extractor.settings,
                "memory_consolidation_cooldown_seconds",
                21600,
            ),
        ):
            return None
        minimum = getattr(self.extractor.settings, "memory_consolidation_min_memories", 6)
        candidates = tuple(
            MemoryConsolidationSource(
                id=int(memory.id),
                memory_kind=memory.memory_kind,
                predicate=memory.predicate,
                object_text=memory.object_text,
                summary=memory.summary,
            )
            for memory in (
                await session.scalars(
                    select(Memory)
                    .where(
                        Memory.user_id == user_id,
                        Memory.visibility == MemoryVisibility.PRIVATE.value,
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.memory_kind.in_(
                            [MemoryKind.FACT.value, MemoryKind.EPISODE.value]
                        ),
                    )
                    .order_by(desc(Memory.updated_at), desc(Memory.created_at))
                    .limit(MAX_CONSOLIDATION_SOURCES)
                )
            ).all()
        )
        if len(candidates) < minimum:
            return None
        return MemoryConsolidationPlan(
            user_id=user_id,
            guild_id=guild_id,
            current_time=current_time,
            state_key=state_key,
            sources=candidates,
        )

    async def generate_memory_consolidation(
        self,
        plan: MemoryConsolidationPlan,
    ) -> tuple[MemoryConsolidationDecision | None, LLMResult | None]:
        availability_check = getattr(self.llm_client, "is_model_available", None)
        if callable(availability_check) and not availability_check(
            self.extractor.settings.openai_memory_model
        ):
            return None, None

        memory_lines = "\n".join(
            f"- id={source.id}; kind={source.memory_kind}; predicate={source.predicate or '(legacy)'}; "
            f"summary={source.summary}"
            for source in plan.sources
        )
        try:
            result = await self.llm_client.complete_chat(
                model=self.extractor.settings.openai_memory_model,
                feature="memory_consolidate",
                max_tokens=480,
                temperature=0,
                request_timeout_seconds=8.0,
                request_max_retries=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Consolidate durable memory facts into one compact overview only when at least two facts "
                            "form a coherent, useful picture. Do not infer new facts, merge contradictions, include "
                            "sensitive details, or erase source facts. Return JSON only with keys: "
                            "should_consolidate, summary, source_ids, related_entities."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Active memories for user_id={plan.user_id}:\n{memory_lines}\n\n"
                            "Keep summary under 480 characters and choose 2-12 source IDs from this list."
                        ),
                    },
                ],
            )
        except Exception as exc:
            LOGGER.warning(
                "Memory consolidation deferred after provider failure: %s",
                " ".join(str(exc).split())[:240],
            )
            return None, None
        payload = parse_json_object_payload(result.text)
        if not payload or not coerce_json_bool(payload.get("should_consolidate")):
            return None, result
        summary = " ".join(str(payload.get("summary", "")).split())[
            :MAX_CONSOLIDATED_SUMMARY_CHARS
        ]
        if (
            not summary
            or contains_sensitive_pattern(summary)
            or contains_transient_memory_pattern(summary)
        ):
            return None, result
        allowed_ids = {source.id for source in plan.sources}
        source_ids = [
            source_id
            for value in payload.get("source_ids", [])
            if isinstance(value, int) and not isinstance(value, bool)
            if (source_id := int(value)) in allowed_ids
        ]
        source_ids = list(dict.fromkeys(source_ids))[:12]
        if len(source_ids) < 2:
            return None, result
        return (
            MemoryConsolidationDecision(
                summary=summary,
                source_ids=tuple(source_ids),
                related_entities=normalize_related_entities(payload.get("related_entities")),
            ),
            result,
        )

    async def apply_memory_consolidation(
        self,
        session: AsyncSession,
        *,
        plan: MemoryConsolidationPlan,
        decision: MemoryConsolidationDecision | None,
    ) -> Memory | None:
        if not await self.is_enabled(session, plan.user_id):
            return None
        await self._touch_consolidation_state(
            session,
            key=plan.state_key,
            when=plan.current_time,
        )
        if decision is None:
            return None
        candidates = list(
            (
                await session.scalars(
                    select(Memory).where(
                        Memory.id.in_(decision.source_ids),
                        Memory.user_id == plan.user_id,
                        Memory.visibility == MemoryVisibility.PRIVATE.value,
                        Memory.status == ACTIVE_MEMORY_STATUS,
                        Memory.memory_kind.in_(
                            [MemoryKind.FACT.value, MemoryKind.EPISODE.value]
                        ),
                    )
                )
            ).all()
        )
        planned_sources = {source.id: source for source in plan.sources}
        active_ids = {
            memory.id
            for memory in candidates
            if (planned := planned_sources.get(int(memory.id))) is not None
            and memory.memory_kind == planned.memory_kind
            and memory.predicate == planned.predicate
            and memory.object_text == planned.object_text
            and memory.summary == planned.summary
        }
        source_ids = [
            source_id for source_id in decision.source_ids if source_id in active_ids
        ]
        if len(source_ids) < 2:
            return None
        existing = await self._find_active_typed_memory(
            session,
            user_id=plan.user_id,
            guild_id=plan.guild_id,
            visibility=MemoryVisibility.PRIVATE,
            memory_kind=MemoryKind.SUMMARY,
            subject_key=f"user:{plan.user_id}",
            predicate="consolidated_context",
        )
        supersedes_id = None
        consolidation_count = 1
        if existing is not None:
            if existing.source_memory_ids == source_ids and existing.summary == decision.summary:
                existing.last_confirmed_at = plan.current_time
                existing.reinforcement_count += 1
                await session.flush()
                return existing
            existing.status = MemoryStatus.CONSOLIDATED.value
            existing.valid_until = plan.current_time
            existing.updated_at = plan.current_time
            supersedes_id = existing.id
            consolidation_count = (existing.consolidation_count or 0) + 1
        consolidated = Memory(
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            visibility=MemoryVisibility.PRIVATE.value,
            category="project",
            memory_kind=MemoryKind.SUMMARY.value,
            status=MemoryStatus.ACTIVE.value,
            subject_key=f"user:{plan.user_id}",
            predicate="consolidated_context",
            object_text=decision.summary,
            summary=decision.summary,
            source_excerpt=None,
            tags=["consolidated"],
            confidence=min(
                sum(memory.confidence for memory in candidates if memory.id in source_ids)
                / len(source_ids),
                1.0,
            ),
            reinforcement_count=1,
            consolidation_count=consolidation_count,
            related_entities=list(decision.related_entities),
            source_memory_ids=source_ids,
            supersedes_id=supersedes_id,
            valid_from=plan.current_time,
            last_confirmed_at=plan.current_time,
            updated_at=plan.current_time,
        )
        session.add(consolidated)
        await session.flush()
        return consolidated

    @staticmethod
    async def _consolidation_is_on_cooldown(
        session: AsyncSession,
        *,
        state_key: str,
        now: datetime,
        cooldown_seconds: int | None = None,
    ) -> bool:
        state = await session.get(AppState, state_key)
        if state is None:
            return False
        try:
            last_run = datetime.fromisoformat(state.value)
        except ValueError:
            return False
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        cooldown = 21600 if cooldown_seconds is None else cooldown_seconds
        return (now - last_run.astimezone(timezone.utc)).total_seconds() < cooldown

    @staticmethod
    async def _touch_consolidation_state(
        session: AsyncSession,
        *,
        key: str,
        when: datetime,
    ) -> None:
        state = await session.get(AppState, key)
        value = when.astimezone(timezone.utc).isoformat()
        if state is None:
            session.add(AppState(key=key, value=value))
        else:
            state.value = value
        await session.flush()
