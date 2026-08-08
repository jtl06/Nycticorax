from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import re
from typing import Iterable

from nycti.memory.filtering import contains_sensitive_pattern
from nycti.memory.lifecycle import ACTIVE_MEMORY_STATUS, MemoryKind
from nycti.memory.visibility import MemoryVisibility


USER_SNAPSHOT_SCOPE = "user"
GUILD_SNAPSHOT_SCOPE = "guild"
MAX_SNAPSHOT_CANDIDATES = 300
_HIGH_VALUE_CATEGORIES = frozenset(
    {"identity", "preference", "project", "plan", "routine", "relationship", "lore"}
)
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class MemorySnapshotBuild:
    content_md: str
    source_memory_ids: tuple[int, ...]
    fingerprint: str

    @property
    def item_count(self) -> int:
        return len(self.source_memory_ids)


def memory_snapshot_scope_key(
    scope_type: str,
    *,
    user_id: int | None = None,
    guild_id: int | None = None,
) -> str:
    if scope_type == USER_SNAPSHOT_SCOPE and user_id is not None:
        return f"user:{user_id}:guild:{guild_id if guild_id is not None else 'dm'}"
    if scope_type == GUILD_SNAPSHOT_SCOPE and guild_id is not None:
        return f"guild:{guild_id}"
    raise ValueError("Invalid memory snapshot scope")


def build_memory_snapshot(
    memories: Iterable[object],
    *,
    scope_type: str,
    max_chars: int,
    now: datetime | None = None,
) -> MemorySnapshotBuild:
    """Build a bounded materialized view without deleting durable source memories."""
    current_time = _as_utc(now or datetime.now(timezone.utc))
    candidates = [
        memory
        for memory in memories
        if _eligible(memory, scope_type=scope_type, now=current_time)
    ]
    active_ids = {int(getattr(memory, "id")) for memory in candidates}
    valid_summaries = {
        int(getattr(memory, "id")): memory
        for memory in candidates
        if _valid_summary(memory, active_ids=active_ids)
    }
    covered_ids = {
        int(source_id)
        for summary in valid_summaries.values()
        for source_id in (getattr(summary, "source_memory_ids", None) or [])
    }
    compact_candidates = [
        memory
        for memory in candidates
        if int(getattr(memory, "id")) in valid_summaries
        or int(getattr(memory, "id")) not in covered_ids
    ]
    compact_candidates.sort(
        key=lambda memory: (
            -memory_snapshot_score(memory, now=current_time),
            -_timestamp(getattr(memory, "updated_at", None)),
            -int(getattr(memory, "id", 0)),
        )
    )

    lines: list[str] = []
    source_ids: list[int] = []
    normalized_seen: set[str] = set()
    for memory in compact_candidates:
        summary = " ".join(str(getattr(memory, "summary", "")).split())
        normalized = _NORMALIZE_RE.sub(" ", summary.casefold()).strip()
        if not normalized or normalized in normalized_seen:
            continue
        line = _render_snapshot_line(memory, scope_type=scope_type, summary=summary)
        proposed = "\n".join([*lines, line])
        if len(proposed) > max_chars:
            continue
        lines.append(line)
        normalized_seen.add(normalized)
        memory_id = int(getattr(memory, "id"))
        source_ids.append(memory_id)
        if memory_id in valid_summaries:
            source_ids.extend(
                int(source_id)
                for source_id in (getattr(memory, "source_memory_ids", None) or [])
            )

    content = "\n".join(lines)
    unique_source_ids = tuple(dict.fromkeys(source_ids))
    fingerprint_source = f"{scope_type}\0{content}\0{','.join(map(str, unique_source_ids))}"
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    return MemorySnapshotBuild(
        content_md=content,
        source_memory_ids=unique_source_ids,
        fingerprint=fingerprint,
    )


def memory_snapshot_score(memory: object, *, now: datetime) -> float:
    """Rank prompt residency; this score never controls durable deletion."""
    score = max(0.0, min(float(getattr(memory, "confidence", 0.0) or 0.0), 1.0)) * 50
    kind = str(getattr(memory, "memory_kind", MemoryKind.FACT.value) or MemoryKind.FACT.value)
    score += {
        MemoryKind.WORKING.value: 42,
        MemoryKind.LORE.value: 34,
        MemoryKind.SUMMARY.value: 30,
        MemoryKind.FACT.value: 24,
        MemoryKind.EPISODE.value: 8,
    }.get(kind, 4)
    category = str(getattr(memory, "category", "") or "").casefold()
    if category in _HIGH_VALUE_CATEGORIES:
        score += 12
    tags = {str(tag).casefold() for tag in (getattr(memory, "tags", None) or [])}
    if tags.intersection({"explicit", "corrected", "pinned"}):
        score += 18
    reinforcement_count = max(int(getattr(memory, "reinforcement_count", 1) or 1), 1)
    times_retrieved = max(int(getattr(memory, "times_retrieved", 0) or 0), 0)
    score += min(math.log2(reinforcement_count + 1) * 10, 30)
    score += min(math.log2(times_retrieved + 1) * 6, 18)
    activity_at = (
        getattr(memory, "last_confirmed_at", None)
        or getattr(memory, "updated_at", None)
        or getattr(memory, "created_at", None)
    )
    score += _recency_bonus(activity_at, now=now, maximum=24, horizon_days=365)
    score += _recency_bonus(
        getattr(memory, "last_retrieved_at", None),
        now=now,
        maximum=12,
        horizon_days=120,
    )
    return score


def _eligible(memory: object, *, scope_type: str, now: datetime) -> bool:
    if str(getattr(memory, "status", "")) != ACTIVE_MEMORY_STATUS:
        return False
    expires_at = getattr(memory, "expires_at", None)
    valid_until = getattr(memory, "valid_until", None)
    if expires_at is not None and _as_utc(expires_at) <= now:
        return False
    if valid_until is not None and _as_utc(valid_until) <= now:
        return False
    visibility = str(getattr(memory, "visibility", MemoryVisibility.PRIVATE.value))
    if scope_type == USER_SNAPSHOT_SCOPE:
        allowed = visibility == MemoryVisibility.PRIVATE.value
    else:
        allowed = visibility in {
            MemoryVisibility.GUILD_SHARED.value,
            MemoryVisibility.LORE.value,
        }
    summary = str(getattr(memory, "summary", "") or "").strip()
    return bool(allowed and summary and not contains_sensitive_pattern(summary))


def _valid_summary(memory: object, *, active_ids: set[int]) -> bool:
    if str(getattr(memory, "memory_kind", "")) != MemoryKind.SUMMARY.value:
        return False
    source_ids = {
        int(source_id)
        for source_id in (getattr(memory, "source_memory_ids", None) or [])
        if isinstance(source_id, int) and not isinstance(source_id, bool)
    }
    return len(source_ids) >= 2 and source_ids.issubset(active_ids)


def _render_snapshot_line(memory: object, *, scope_type: str, summary: str) -> str:
    kind = str(getattr(memory, "memory_kind", MemoryKind.FACT.value) or MemoryKind.FACT.value)
    category = str(getattr(memory, "category", "general") or "general")
    metadata = f"{kind}; {category}"
    if scope_type == GUILD_SNAPSHOT_SCOPE:
        metadata += f"; owner_user_id={int(getattr(memory, 'user_id'))}"
    return f"- [{metadata}] {summary}"


def _recency_bonus(
    value: datetime | None,
    *,
    now: datetime,
    maximum: float,
    horizon_days: float,
) -> float:
    if value is None:
        return 0.0
    age_days = max((now - _as_utc(value)).total_seconds() / 86400, 0.0)
    return maximum * max(1.0 - age_days / horizon_days, 0.0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> float:
    return _as_utc(value).timestamp() if value is not None else 0.0
