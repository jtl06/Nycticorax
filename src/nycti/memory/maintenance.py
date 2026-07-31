from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import Memory, UserSettings
from nycti.memory.filtering import (
    contains_sensitive_pattern,
    is_shareable_market_configuration,
)
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    build_subject_key,
    normalize_predicate,
)
from nycti.memory.profile import strip_sensitive_profile_lines
from nycti.memory.visibility import MemoryVisibility, normalize_memory_visibility


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceResult:
    sensitive_memories_deleted: int = 0
    sensitive_profile_lines_deleted: int = 0
    legacy_memories_normalized: int = 0
    shared_configurations_promoted: int = 0


async def repair_memory_store(
    session: AsyncSession,
    *,
    now: datetime,
) -> MemoryMaintenanceResult:
    """Idempotently scrub unsafe rows and normalize legacy memory metadata."""

    deleted_memories = 0
    normalized_memories = 0
    promoted_memories = 0
    memories = list((await session.scalars(select(Memory))).all())
    for memory in memories:
        combined_text = "\n".join(
            part
            for part in (memory.summary, memory.source_excerpt or "")
            if part.strip()
        )
        if contains_sensitive_pattern(combined_text):
            await session.delete(memory)
            deleted_memories += 1
            continue
        if memory.status != ACTIVE_MEMORY_STATUS:
            continue

        changed = False
        try:
            visibility = normalize_memory_visibility(memory.visibility)
        except ValueError:
            visibility = MemoryVisibility.PRIVATE
            memory.visibility = visibility.value
            changed = True
        if (
            visibility is MemoryVisibility.PRIVATE
            and memory.guild_id is not None
            and is_shareable_market_configuration(
                summary=memory.summary,
                source_excerpt=memory.source_excerpt or "",
                tags=memory.tags,
            )
        ):
            visibility = MemoryVisibility.GUILD_SHARED
            memory.visibility = visibility.value
            promoted_memories += 1
            changed = True

        try:
            memory_kind = MemoryKind(str(memory.memory_kind or ""))
        except ValueError:
            memory_kind = MemoryKind.LORE if memory.category == "lore" else MemoryKind.FACT
            memory.memory_kind = memory_kind.value
            changed = True
        expected_subject = build_subject_key(
            user_id=memory.user_id,
            guild_id=memory.guild_id,
            kind=memory_kind,
            visibility=visibility.value,
        )
        if memory.subject_key != expected_subject:
            memory.subject_key = expected_subject
            changed = True
        if not str(memory.predicate or "").strip():
            fallback = (
                f"shared_market_configuration_{memory.id}"
                if visibility is MemoryVisibility.GUILD_SHARED
                else f"legacy_{memory.category}_{memory.id}"
            )
            memory.predicate = normalize_predicate(None, fallback=fallback)
            changed = True
        if not str(memory.object_text or "").strip():
            memory.object_text = memory.summary[:320]
            changed = True
        if memory.valid_from is None:
            memory.valid_from = memory.created_at
            changed = True
        if memory.last_confirmed_at is None:
            memory.last_confirmed_at = memory.created_at
            changed = True
        if changed:
            memory.updated_at = now
            normalized_memories += 1

    removed_profile_lines = 0
    settings_rows = list((await session.scalars(select(UserSettings))).all())
    for settings in settings_rows:
        existing = settings.personal_profile_md
        cleaned = strip_sensitive_profile_lines(existing)
        if cleaned == existing:
            continue
        removed_profile_lines += max(
            len([line for line in existing.splitlines() if line.strip()])
            - len([line for line in cleaned.splitlines() if line.strip()]),
            0,
        )
        settings.personal_profile_md = cleaned

    await session.flush()
    return MemoryMaintenanceResult(
        sensitive_memories_deleted=deleted_memories,
        sensitive_profile_lines_deleted=removed_profile_lines,
        legacy_memories_normalized=normalized_memories,
        shared_configurations_promoted=promoted_memories,
    )
