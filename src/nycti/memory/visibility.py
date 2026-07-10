from __future__ import annotations

from enum import StrEnum


class MemoryVisibility(StrEnum):
    """Who may retrieve a stored memory during an agent run."""

    PRIVATE = "private"
    GUILD_SHARED = "guild_shared"
    LORE = "lore"


GUILD_VISIBLE_MEMORY_SCOPES = frozenset(
    {
        MemoryVisibility.GUILD_SHARED,
        MemoryVisibility.LORE,
    }
)


def normalize_memory_visibility(value: MemoryVisibility | str) -> MemoryVisibility:
    if isinstance(value, MemoryVisibility):
        return value
    try:
        return MemoryVisibility(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(scope.value for scope in MemoryVisibility)
        raise ValueError(f"Invalid memory visibility {value!r}; expected one of: {allowed}.") from exc


def can_read_memory(
    *,
    visibility: MemoryVisibility | str,
    owner_user_id: int,
    memory_guild_id: int | None,
    requester_user_id: int,
    requester_guild_id: int | None,
) -> bool:
    """Fail closed unless the requester is allowed to read this exact memory."""

    try:
        normalized = normalize_memory_visibility(visibility)
    except ValueError:
        return False
    if normalized is MemoryVisibility.PRIVATE:
        return owner_user_id == requester_user_id
    return (
        normalized in GUILD_VISIBLE_MEMORY_SCOPES
        and requester_guild_id is not None
        and memory_guild_id == requester_guild_id
    )


def validate_memory_visibility_context(
    visibility: MemoryVisibility | str,
    *,
    guild_id: int | None,
) -> MemoryVisibility:
    normalized = normalize_memory_visibility(visibility)
    if normalized in GUILD_VISIBLE_MEMORY_SCOPES and guild_id is None:
        raise ValueError(f"{normalized.value} memories require a guild context.")
    return normalized
