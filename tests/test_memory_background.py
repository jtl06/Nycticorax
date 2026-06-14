from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from nycti.memory.background import BackgroundMemoryWriter


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeDatabase:
    def __init__(self) -> None:
        self.value = _FakeSession()

    @asynccontextmanager
    async def session(self):
        yield self.value


class _FakeMemberAliases:
    async def list_matching_aliases(self, _session, *, guild_id: int, text: str):
        return [SimpleNamespace(alias="GTS", user_id=2)]


class _FakeMemoryService:
    def __init__(self) -> None:
        self.store_users: list[int] = []
        self.profile_users: list[int] = []

    async def maybe_store_memory(self, _session, *, user_id: int, **_kwargs):
        self.store_users.append(user_id)
        return None, None

    async def maybe_update_personal_profile(self, _session, *, user_id: int, **_kwargs):
        self.profile_users.append(user_id)
        return None


class BackgroundMemoryWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_caller_signal_does_not_update_mentioned_user_profile(self) -> None:
        database = _FakeDatabase()
        memory_service = _FakeMemoryService()
        writer = BackgroundMemoryWriter(
            settings=SimpleNamespace(profile_update_cooldown_seconds=0),
            database=database,
            memory_service=memory_service,
            member_alias_service=_FakeMemberAliases(),
        )

        await writer.run(
            guild_id=1,
            channel_id=2,
            user_id=1,
            source_message_id=3,
            current_message="I prefer dark mode, unlike GTS.",
            recent_context="",
        )

        self.assertEqual([1, 2], memory_service.store_users)
        self.assertEqual([1], memory_service.profile_users)
        self.assertEqual(1, database.value.commits)


if __name__ == "__main__":
    unittest.main()
