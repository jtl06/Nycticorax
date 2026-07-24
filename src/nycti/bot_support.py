from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from nycti.chat.context import PreparedChatContext
from nycti.formatting import format_current_datetime_context

BENCHMARK_USER_ID = 9_000_000_001
BENCHMARK_USER_NAME = "benchmark"
BENCHMARK_TIMEZONE_NAME = "UTC"


def select_human_mentioned_user_ids(
    mentions: Iterable[object],
    *,
    bot_user_id: int,
) -> list[int]:
    selected: list[int] = []
    for mentioned_user in mentions:
        user_id = getattr(mentioned_user, "id", None)
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id == bot_user_id
            or bool(getattr(mentioned_user, "bot", False))
            or user_id in selected
        ):
            continue
        selected.append(user_id)
    return selected


def build_isolated_benchmark_context(
    *,
    now: datetime | None = None,
    personal_profile_block: str = "",
    memories_block: str = "",
) -> PreparedChatContext:
    """Build evaluation context without consulting Discord or user storage."""
    return PreparedChatContext(
        current_datetime_text=format_current_datetime_context(
            now or datetime.now(timezone.utc),
            BENCHMARK_TIMEZONE_NAME,
        ),
        memories_block=memories_block.strip() or "(none)",
        personal_profile_block=personal_profile_block.strip() or "(none)",
        channel_alias_block="(none configured)",
        member_alias_block="(none matched)",
        mentioned_user_memories_block="(none)",
        memory_enabled=bool(personal_profile_block.strip() or memories_block.strip()),
        retrieved_memories=[],
        memory_retrieval_ms=0,
    )
