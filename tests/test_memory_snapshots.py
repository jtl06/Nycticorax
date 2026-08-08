from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from nycti.memory.snapshots import (
    GUILD_SNAPSHOT_SCOPE,
    USER_SNAPSHOT_SCOPE,
    build_memory_snapshot,
    memory_snapshot_score,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _memory(memory_id: int, summary: str, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": memory_id,
        "user_id": 1,
        "visibility": "private",
        "category": "preference",
        "memory_kind": "fact",
        "status": "active",
        "summary": summary,
        "source_memory_ids": None,
        "confidence": 0.9,
        "reinforcement_count": 1,
        "times_retrieved": 0,
        "last_confirmed_at": NOW,
        "last_retrieved_at": None,
        "updated_at": NOW,
        "created_at": NOW,
        "expires_at": None,
        "valid_until": None,
        "tags": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MemorySnapshotPolicyTests(unittest.TestCase):
    def test_reinforced_retrieved_fact_outranks_weak_recent_chatter(self) -> None:
        durable = _memory(
            1,
            "Uses Helix for Python projects",
            reinforcement_count=5,
            times_retrieved=8,
            last_confirmed_at=NOW - timedelta(days=90),
            updated_at=NOW - timedelta(days=90),
        )
        weak = _memory(
            2,
            "Mentioned a one-off lunch choice",
            memory_kind="episode",
            category="other",
            confidence=0.55,
        )

        self.assertGreater(
            memory_snapshot_score(durable, now=NOW),
            memory_snapshot_score(weak, now=NOW),
        )

    def test_compaction_evicts_only_from_bounded_view(self) -> None:
        memories = [
            _memory(index, f"Durable preference number {index}", reinforcement_count=6 - index)
            for index in range(1, 6)
        ]

        built = build_memory_snapshot(
            memories,
            scope_type=USER_SNAPSHOT_SCOPE,
            max_chars=105,
            now=NOW,
        )

        self.assertLess(len(built.source_memory_ids), len(memories))
        self.assertLessEqual(len(built.content_md), 105)
        self.assertEqual(5, len(memories))

    def test_valid_summary_replaces_covered_source_bullets(self) -> None:
        sources = [
            _memory(1, "Builds Nycti"),
            _memory(2, "Uses Python"),
        ]
        summary = _memory(
            3,
            "Builds Nycti in Python",
            memory_kind="summary",
            category="project",
            source_memory_ids=[1, 2],
        )

        built = build_memory_snapshot(
            [*sources, summary],
            scope_type=USER_SNAPSHOT_SCOPE,
            max_chars=500,
            now=NOW,
        )

        self.assertIn("Builds Nycti in Python", built.content_md)
        self.assertNotIn("] Builds Nycti\n", built.content_md)
        self.assertEqual({1, 2, 3}, set(built.source_memory_ids))

    def test_expired_superseded_and_wrong_visibility_are_excluded(self) -> None:
        active_private = _memory(1, "Private active")
        expired = _memory(2, "Expired", expires_at=NOW - timedelta(seconds=1))
        superseded = _memory(3, "Superseded", status="superseded")
        shared = _memory(4, "Server convention", visibility="guild_shared")

        user = build_memory_snapshot(
            [active_private, expired, superseded, shared],
            scope_type=USER_SNAPSHOT_SCOPE,
            max_chars=500,
            now=NOW,
        )
        guild = build_memory_snapshot(
            [active_private, expired, superseded, shared],
            scope_type=GUILD_SNAPSHOT_SCOPE,
            max_chars=500,
            now=NOW,
        )

        self.assertIn("Private active", user.content_md)
        self.assertNotIn("Server convention", user.content_md)
        self.assertIn("Server convention", guild.content_md)
        self.assertNotIn("Private active", guild.content_md)
        self.assertNotIn("Expired", f"{user.content_md}\n{guild.content_md}")
        self.assertNotIn("Superseded", f"{user.content_md}\n{guild.content_md}")
