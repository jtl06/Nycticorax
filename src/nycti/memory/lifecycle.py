from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
import re
from typing import Iterable

from nycti.memory.filtering import tokenize


class MemoryKind(StrEnum):
    FACT = "fact"
    EPISODE = "episode"
    WORKING = "working"
    SUMMARY = "summary"
    LORE = "lore"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    CONSOLIDATED = "consolidated"


class MemoryOperation(StrEnum):
    UPSERT = "upsert"
    RETRACT = "retract"


ACTIVE_MEMORY_STATUS = MemoryStatus.ACTIVE.value
MAX_RELATED_ENTITIES = 6
MAX_ENTITY_CHARS = 64
MAX_PREDICATE_CHARS = 64
_KEY_PART_RE = re.compile(r"[^a-z0-9]+")
_DEEP_MEMORY_TERMS = frozenset(
    {
        "memory",
        "memories",
        "remember",
        "remembered",
        "history",
        "before",
        "previously",
        "relationship",
        "relationships",
        "list",
        "watchlist",
    }
)
_PERSONAL_MEMORY_TERMS = frozenset(
    {
        "i",
        "im",
        "me",
        "my",
        "mine",
        "we",
        "our",
        "again",
        "prefer",
        "favorite",
        "project",
        "plan",
        "goal",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryRetrievalPlan:
    limit: int
    include_history: bool


def build_memory_retrieval_plan(
    prompt: str,
    *,
    maximum: int,
) -> MemoryRetrievalPlan:
    """Choose a small context budget from the request, never above configuration."""

    bounded_maximum = max(1, maximum)
    tokens = set(tokenize(prompt))
    if tokens.intersection(_DEEP_MEMORY_TERMS):
        limit = bounded_maximum
    elif tokens.intersection(_PERSONAL_MEMORY_TERMS):
        limit = min(bounded_maximum, 4)
    else:
        limit = min(bounded_maximum, 2)
    return MemoryRetrievalPlan(
        limit=limit,
        include_history=bool(tokens.intersection({"before", "previously", "history", "used", "former"})),
    )


def normalize_memory_kind(value: object, *, category: str) -> MemoryKind:
    cleaned = str(value or "").strip().lower()
    if category == "lore":
        return MemoryKind.LORE
    try:
        kind = MemoryKind(cleaned)
    except ValueError:
        return MemoryKind.FACT
    if kind in {MemoryKind.LORE, MemoryKind.SUMMARY}:
        return MemoryKind.FACT
    return kind


def normalize_memory_operation(value: object) -> MemoryOperation:
    try:
        return MemoryOperation(str(value or "").strip().lower())
    except ValueError:
        return MemoryOperation.UPSERT


def normalize_predicate(value: object, *, fallback: str) -> str:
    cleaned = _normalize_key_part(str(value or ""))
    if not cleaned:
        cleaned = _normalize_key_part(fallback)
    return cleaned[:MAX_PREDICATE_CHARS] or "general_fact"


def normalize_related_entities(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    normalized: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).strip().split())
        if not cleaned:
            continue
        entity = _normalize_key_part(cleaned)[:MAX_ENTITY_CHARS]
        if entity and entity not in normalized:
            normalized.append(entity)
        if len(normalized) >= MAX_RELATED_ENTITIES:
            break
    return tuple(normalized)


def build_subject_key(
    *,
    user_id: int,
    guild_id: int | None,
    kind: MemoryKind,
    visibility: str,
) -> str:
    if kind is MemoryKind.LORE and visibility != "private" and guild_id is not None:
        return f"guild:{guild_id}"
    if visibility != "private" and guild_id is not None:
        return f"guild:{guild_id}:user:{user_id}"
    return f"user:{user_id}"


def effective_memory_confidence(
    memory: object,
    *,
    now: datetime | None = None,
    half_life_days: int = 365,
) -> float:
    current = _as_utc(now or datetime.now(timezone.utc))
    confidence = min(max(float(getattr(memory, "confidence", 0.0) or 0.0), 0.0), 1.0)
    confirmed_at = (
        getattr(memory, "last_confirmed_at", None)
        or getattr(memory, "valid_from", None)
        or getattr(memory, "created_at", current)
    )
    age_days = max((current - _as_utc(confirmed_at)).total_seconds() / 86_400, 0.0)
    kind = str(getattr(memory, "memory_kind", MemoryKind.FACT.value) or MemoryKind.FACT.value)
    effective_half_life = max(half_life_days, 1)
    if kind == MemoryKind.WORKING.value:
        effective_half_life = min(effective_half_life, 14)
    elif kind == MemoryKind.EPISODE.value:
        effective_half_life = min(effective_half_life, 120)
    elif kind in {MemoryKind.SUMMARY.value, MemoryKind.LORE.value}:
        effective_half_life = max(effective_half_life, 540)
    category = str(getattr(memory, "category", "") or "").casefold()
    tags = {str(tag).casefold() for tag in (getattr(memory, "tags", None) or [])}
    if category in {"plan", "routine"} and not tags.intersection(
        {"explicit", "corrected", "pinned"}
    ):
        effective_half_life = min(effective_half_life, 120)
    decay = math.pow(0.5, age_days / effective_half_life)
    reinforcement_count = max(int(getattr(memory, "reinforcement_count", 1) or 1), 1)
    reinforcement_bonus = min(math.log2(reinforcement_count) * 0.025, 0.12)
    return min(confidence * decay + reinforcement_bonus, 1.0)


def memory_is_active(memory: object, *, now: datetime | None = None) -> bool:
    current = _as_utc(now or datetime.now(timezone.utc))
    if str(getattr(memory, "status", ACTIVE_MEMORY_STATUS) or ACTIVE_MEMORY_STATUS) != ACTIVE_MEMORY_STATUS:
        return False
    valid_from = getattr(memory, "valid_from", None)
    if valid_from is not None and _as_utc(valid_from) > current:
        return False
    for field_name in ("valid_until", "expires_at"):
        value = getattr(memory, field_name, None)
        if value is not None and _as_utc(value) <= current:
            return False
    return True


def memory_entity_overlap(query: str, entities: Iterable[str]) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    entity_tokens = set(tokenize(" ".join(entities)))
    if not entity_tokens:
        return 0.0
    return len(query_tokens.intersection(entity_tokens)) / max(len(entity_tokens), 1)


def _normalize_key_part(value: str) -> str:
    return _KEY_PART_RE.sub("_", value.strip().lower()).strip("_")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
