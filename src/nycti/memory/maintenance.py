from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import MemberAlias, MemberIdentity, Memory, UserSettings
from nycti.memory.filtering import (
    contains_sensitive_pattern,
    is_shareable_market_configuration,
)
from nycti.memory.lifecycle import (
    ACTIVE_MEMORY_STATUS,
    MemoryKind,
    MemoryStatus,
    build_subject_key,
    normalize_predicate,
)
from nycti.memory.profile import strip_sensitive_profile_lines
from nycti.memory.visibility import MemoryVisibility, normalize_memory_visibility


LEGACY_SHARED_MARKET_PREFIX = "shared_market_configuration_"
SHARED_MARKET_TICKER_PREFIX = "shared_market_report_ticker_"
PERSONAL_MARKET_TICKER_PREFIX = "stock_ticker_interest_"
_TICKER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?P<dollar>\$?)(?P<symbol>[A-Za-z][A-Za-z0-9.-]{0,8})(?![A-Za-z0-9])")
_MARKET_SCOPE_RE = re.compile(
    r"\b(?:market|markets|stock|stocks|ticker|tickers|watchlist|report|reports|queries)\b",
    re.IGNORECASE,
)
_TICKER_STOPWORDS = frozenset(
    {
        "AI",
        "API",
        "CEO",
        "ETF",
        "FUTURE",
        "INCLUDE",
        "MARKET",
        "REPORT",
        "STOCK",
        "TICKER",
        "USD",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceResult:
    sensitive_memories_deleted: int = 0
    sensitive_profile_lines_deleted: int = 0
    legacy_memories_normalized: int = 0
    shared_configurations_promoted: int = 0
    shared_watchlist_symbols_migrated: int = 0
    legacy_market_configurations_retired: int = 0


async def repair_memory_store(
    session: AsyncSession,
    *,
    now: datetime,
) -> MemoryMaintenanceResult:
    """Idempotently scrub unsafe rows and normalize legacy memory metadata."""

    deleted_memories = 0
    normalized_memories = 0
    promoted_memories = 0
    migrated_watchlist_symbols = 0
    retired_market_configurations = 0
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

    member_tokens_by_guild = await _member_tokens_by_guild(session)
    known_symbols_by_guild = _known_ticker_symbols(memories)
    existing_shared_symbols = {
        (int(memory.guild_id), str(memory.object_text or "").strip().upper())
        for memory in memories
        if memory.guild_id is not None
        and memory.status == ACTIVE_MEMORY_STATUS
        and str(memory.predicate or "").startswith(SHARED_MARKET_TICKER_PREFIX)
        and str(memory.object_text or "").strip()
    }
    for memory in memories:
        if (
            memory.guild_id is None
            or memory.status != ACTIVE_MEMORY_STATUS
            or memory.visibility != MemoryVisibility.GUILD_SHARED.value
            or not str(memory.predicate or "").startswith(LEGACY_SHARED_MARKET_PREFIX)
        ):
            continue
        guild_id = int(memory.guild_id)
        symbols = _legacy_market_symbols(
            memory,
            known_symbols=known_symbols_by_guild.get(guild_id, set()),
            member_tokens=member_tokens_by_guild.get(guild_id, set()),
        )
        for symbol in symbols:
            identity = (guild_id, symbol)
            if identity in existing_shared_symbols:
                continue
            predicate = f"{SHARED_MARKET_TICKER_PREFIX}{symbol.casefold()}"
            session.add(
                Memory(
                    guild_id=guild_id,
                    channel_id=memory.channel_id,
                    user_id=memory.user_id,
                    source_message_id=memory.source_message_id,
                    visibility=MemoryVisibility.GUILD_SHARED.value,
                    category="preference",
                    memory_kind=MemoryKind.FACT.value,
                    status=ACTIVE_MEMORY_STATUS,
                    subject_key=build_subject_key(
                        user_id=memory.user_id,
                        guild_id=guild_id,
                        kind=MemoryKind.FACT,
                        visibility=MemoryVisibility.GUILD_SHARED.value,
                    ),
                    predicate=predicate,
                    object_text=symbol,
                    summary=f"Include {symbol} in shared market reports",
                    source_excerpt=memory.source_excerpt,
                    tags=["stock", "ticker", "shared_watchlist", symbol.casefold()],
                    confidence=memory.confidence,
                    reinforcement_count=max(memory.reinforcement_count or 1, 1),
                    related_entities=[symbol.casefold()],
                    valid_from=memory.valid_from or memory.created_at,
                    last_confirmed_at=memory.last_confirmed_at or memory.created_at,
                    updated_at=now,
                )
            )
            existing_shared_symbols.add(identity)
            known_symbols_by_guild.setdefault(guild_id, set()).add(symbol)
            migrated_watchlist_symbols += 1
        memory.status = MemoryStatus.CONSOLIDATED.value
        memory.valid_until = now
        memory.updated_at = now
        retired_market_configurations += 1

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
        shared_watchlist_symbols_migrated=migrated_watchlist_symbols,
        legacy_market_configurations_retired=retired_market_configurations,
    )


async def _member_tokens_by_guild(session: AsyncSession) -> dict[int, set[str]]:
    by_guild: dict[int, set[str]] = {}
    aliases = list((await session.scalars(select(MemberAlias))).all())
    identities = list((await session.scalars(select(MemberIdentity))).all())
    for guild_id, values in [
        *[(alias.guild_id, (alias.alias,)) for alias in aliases],
        *[
            (
                identity.guild_id,
                (identity.username, identity.global_name, identity.display_name),
            )
            for identity in identities
        ],
    ]:
        tokens = by_guild.setdefault(int(guild_id), set())
        for value in values:
            tokens.update(
                token.upper()
                for token in re.findall(r"[A-Za-z][A-Za-z0-9]{1,15}", value or "")
            )
    return by_guild


def _known_ticker_symbols(memories: list[Memory]) -> dict[int, set[str]]:
    by_guild: dict[int, set[str]] = {}
    for memory in memories:
        if memory.guild_id is None or memory.status != ACTIVE_MEMORY_STATUS:
            continue
        predicate = str(memory.predicate or "")
        if not predicate.startswith(
            (PERSONAL_MARKET_TICKER_PREFIX, SHARED_MARKET_TICKER_PREFIX)
        ):
            continue
        symbol = str(memory.object_text or "").strip().removeprefix("$").upper()
        if symbol:
            by_guild.setdefault(int(memory.guild_id), set()).add(symbol)
    return by_guild


def _legacy_market_symbols(
    memory: Memory,
    *,
    known_symbols: set[str],
    member_tokens: set[str],
) -> tuple[str, ...]:
    source = str(memory.source_excerpt or "")
    combined = f"{memory.summary}\n{source}"
    source_has_market_scope = _MARKET_SCOPE_RE.search(source) is not None
    symbols: list[str] = []
    for match in _TICKER_TOKEN_RE.finditer(combined):
        raw = match.group("symbol")
        symbol = raw.upper()
        explicit_dollar = bool(match.group("dollar"))
        if symbol in _TICKER_STOPWORDS:
            continue
        if not explicit_dollar and not raw.isupper() and symbol not in known_symbols:
            continue
        if _matches_member_token(symbol, member_tokens) and not re.search(
            rf"(?<![A-Za-z0-9])\${re.escape(symbol)}(?![A-Za-z0-9])",
            source,
            re.IGNORECASE,
        ):
            continue
        resolved = _resolve_known_symbol(symbol, known_symbols)
        if (
            resolved not in known_symbols
            and not explicit_dollar
            and not source_has_market_scope
        ):
            continue
        if resolved not in symbols:
            symbols.append(resolved)
    return tuple(symbols[:12])


def _matches_member_token(symbol: str, member_tokens: set[str]) -> bool:
    return any(re.fullmatch(rf"{re.escape(symbol)}\d*", token) for token in member_tokens)


def _resolve_known_symbol(symbol: str, known_symbols: set[str]) -> str:
    if symbol in known_symbols:
        return symbol
    candidates = [
        known
        for known in known_symbols
        if _is_single_adjacent_transposition(symbol, known)
    ]
    return candidates[0] if len(candidates) == 1 else symbol


def _is_single_adjacent_transposition(left: str, right: str) -> bool:
    if len(left) != len(right) or left == right:
        return False
    differences = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
    return bool(
        len(differences) == 2
        and differences[1] == differences[0] + 1
        and left[differences[0]] == right[differences[1]]
        and left[differences[1]] == right[differences[0]]
    )
