from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.changelog_service import ChangelogService


class _FakeColumn:
    def like(self, pattern: str) -> tuple[str, str]:
        return ("like", pattern)


class _FakeState:
    key = _FakeColumn()

    def __init__(self, *, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _FakeStatement:
    def where(self, _condition: object) -> _FakeStatement:
        return self


class _FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def all(self) -> list[object]:
        return self.values


class _FakeSession:
    def __init__(self) -> None:
        self.states: dict[str, object] = {}
        self.flushes = 0

    async def get(self, _model: object, key: str) -> object | None:
        return self.states.get(key)

    def add(self, state: object) -> None:
        self.states[state.key] = state

    async def delete(self, state: object) -> None:
        self.states.pop(state.key, None)

    async def flush(self) -> None:
        self.flushes += 1

    async def scalars(self, _statement: object) -> _FakeScalarResult:
        return _FakeScalarResult(list(self.states.values()))


class _FakeChannel:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class ChangelogServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, *, channel: object | None = None):
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=None,
        )
        return ChangelogService(
            bot=bot,
            database=SimpleNamespace(),
            settings=SimpleNamespace(),
            state_model=_FakeState,
            select_factory=lambda _model: _FakeStatement(),
        )

    async def test_channel_and_snapshot_state_round_trip(self) -> None:
        service = self._service()
        session = _FakeSession()

        await service.set_channel_id(session, guild_id=7, channel_id=123)
        await service.set_last_snapshot(session, guild_id=7, snapshot="release-1")

        self.assertEqual(123, await service.get_channel_id(session, guild_id=7))
        self.assertEqual("release-1", await service.get_last_snapshot(session, guild_id=7))
        self.assertEqual([(7, 123)], await service.list_configured_channels(session))

        await service.set_channel_id(session, guild_id=7, channel_id=None)
        self.assertIsNone(await service.get_channel_id(session, guild_id=7))

    async def test_post_announcement_uses_cached_channel(self) -> None:
        channel = _FakeChannel()
        service = self._service(channel=channel)

        sent = await service.post_announcement(123, "Shipped the new loop.")

        self.assertTrue(sent)
        self.assertEqual(["Shipped the new loop."], channel.messages)

    async def test_post_announcement_chunks_content_within_discord_limit(self) -> None:
        channel = _FakeChannel()
        service = self._service(channel=channel)
        content = "# Changelog\n\n" + "\n".join(
            f"- change {index}: " + ("x" * 220)
            for index in range(20)
        )

        sent = await service.post_announcement(123, content)

        self.assertTrue(sent)
        self.assertGreater(len(channel.messages), 1)
        self.assertTrue(all(len(message) <= 1900 for message in channel.messages))
        self.assertEqual(
            content.replace("\n\n", "").replace("\n", ""),
            "".join(channel.messages).replace("\n\n", "").replace("\n", ""),
        )

    def test_state_keys_are_guild_scoped(self) -> None:
        self.assertEqual("changelog_channel_id:7", ChangelogService.channel_key(7))
        self.assertEqual("last_changelog_snapshot:7", ChangelogService.snapshot_key(7))


if __name__ == "__main__":
    unittest.main()
