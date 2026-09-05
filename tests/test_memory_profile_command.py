from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nycti.discord.memory import register_memory_commands


class ProfileCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = {}
        def command(*, name, **_kwargs):
            def decorate(function):
                self.commands[name] = function
                return function
            return decorate
        self.session = SimpleNamespace(commit=AsyncMock())
        @asynccontextmanager
        async def session():
            yield self.session
        self.service = SimpleNamespace(apply_personal_profile_update=AsyncMock(return_value=True))
        self.bot = SimpleNamespace(
            tree=SimpleNamespace(command=command),
            database=SimpleNamespace(session=session),
            memory_service=self.service,
            settings=SimpleNamespace(discord_admin_user_id=None),
        )
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=AsyncMock()),
        )
        register_memory_commands(self.bot)

    async def test_explicit_profile_edit_uses_existing_memory_service(self):
        await self.commands["memory"](self.interaction, profile_text="I prefer short replies.")
        self.service.apply_personal_profile_update.assert_awaited_once_with(
            self.session, user_id=1, profile_md="I prefer short replies.",
        )
        self.session.commit.assert_awaited_once()

    async def test_non_owner_cannot_edit_someone_elses_profile(self):
        await self.commands["memory"](self.interaction, userid="2", profile_text="Changed note")
        self.service.apply_personal_profile_update.assert_not_awaited()

    async def test_profile_edit_rejects_oversized_and_conflicting_actions(self):
        await self.commands["memory"](self.interaction, profile_text="x" * 1601)
        await self.commands["memory"](self.interaction, enable=True, profile_text="Changed note")
        self.service.apply_personal_profile_update.assert_not_awaited()
