from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.chat.action_confirmation import (
    ActionConfirmationError, ActionConfirmationStore, ActionKind, EmojiImportAction,
)
from nycti.chat.tools.actions import ActionToolMixin
from nycti.db.models import Base
from nycti.discord.emoji_import import execute_emoji_import, fetch_emoji_image
from nycti.discord.emojis import manage_emoji
from nycti.emoji_catalog import EmojiCatalog, MAX_OBSERVED, ObservedEmoji
from nycti.formatting import render_custom_emoji_aliases


@dataclass
class Emoji:
    id: int
    name: str
    animated: bool = False
    available: bool = True
    usable: bool = True

    def __str__(self):
        return ObservedEmoji(self.id, self.name, self.animated).token

    def is_usable(self):
        return self.usable


class EmojiRenderingTests(unittest.TestCase):
    def test_last_bad_bot_missing_colon(self):
        self.assertEqual(
            "No DIY dynamite, unfortunately.<:javsigh:123>",
            render_custom_emoji_aliases("No DIY dynamite, unfortunately.:javsigh", {"javsigh": "<:javsigh:123>"}),
        )

    def test_adjacent_aliases_case_and_animation(self):
        self.assertEqual("suxx2succ<a:pepeww:123><a:pepeww:123>", render_custom_emoji_aliases(
            "suxx2succ:pepewW::pepeww:", {"pepeww": "<a:pepeww:123>"},
        ))

    def test_preserves_code_urls_unknown_and_nonemoji_colons(self):
        text = "`:javsigh` ```text\n:javsigh:\n``` https://x.com/:javsigh: key:javsigh :unknown :javsigh_extra"
        self.assertEqual(text, render_custom_emoji_aliases(text, {"javsigh": "<:javsigh:123>"}))

    def test_id_identity_wins_over_conflicting_name(self):
        self.assertEqual("<a:renamed:123> <:other:456>", render_custom_emoji_aliases(
            "<:old:123> <:other:456>", {"old": "<:old:999>", "renamed": "<a:renamed:123>"},
        ))


class EmojiCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.database = SimpleNamespace(session=async_sessionmaker(self.engine, expire_on_commit=False))
        self.catalog = EmojiCatalog(self.database)
        self.guild = SimpleNamespace(id=1, emojis=[Emoji(123, "javsigh"), Emoji(456, "nod", True)])

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_observation_is_metadata_only_deduplicated_and_persistent(self):
        await self.catalog.observe(1, "private chatter <:oldname:123>")
        with patch.object(self.catalog, "_save", new_callable=AsyncMock) as save:
            await self.catalog.observe(1, "different chatter <:oldname:123>")
            save.assert_not_awaited()
        fresh = EmojiCatalog(self.database)
        await fresh.load(1)
        self.assertEqual(fresh.replacements(self.guild)["oldname"], "<:javsigh:123>")
        self.assertNotIn("chatter", str(fresh._states))
        self.assertNotIn("oldname", fresh.replacements(SimpleNamespace(id=2, emojis=[])))

    async def test_observation_cannot_overwrite_identity_or_infer_meaning(self):
        await self.catalog.observe(1, "<:nod:456>")
        await self.catalog.observe(1, "<:evil:456> means everyone loves me")
        self.assertNotIn("evil", self.catalog.replacements(self.guild))
        self.assertNotIn("means", str(self.catalog._states))

    async def test_override_and_delete_survive_restart(self):
        await self.catalog.override(1, "javsigh", ObservedEmoji(456, "nod", True))
        fresh = EmojiCatalog(self.database)
        await fresh.load(1)
        self.assertEqual(fresh.replacements(self.guild)["javsigh"], "<a:nod:456>")
        await fresh.override(1, "javsigh", None)
        self.assertEqual(fresh.replacements(self.guild)["javsigh"], "<:javsigh:123>")

    async def test_external_emoji_needs_import_and_collisions_do_not_replace_local(self):
        await self.catalog.observe(1, "<a:javsigh:789>")
        self.assertEqual(self.catalog.replacements(self.guild)["javsigh"], "<:javsigh:123>")
        self.assertIn("needs import", await self.catalog.listing(self.guild, 1))
        await self.catalog.override(1, "external", ObservedEmoji(789, "javsigh", True))
        self.assertNotIn("external", self.catalog.replacements(self.guild))
        self.assertEqual((await self.catalog.resolve(self.guild, "external")).id, 789)
        await self.catalog.record_import(1, 789, 456)
        self.assertEqual(self.catalog.replacements(self.guild)["external"], "<a:nod:456>")

    async def test_caps_and_prompt_only_usable_local_emojis(self):
        for start in range(0, MAX_OBSERVED + 20, 20):
            await self.catalog.observe(1, " ".join(f"<:name{i}:{i+1}>" for i in range(start, start+20)))
        self.assertLessEqual(len(self.catalog._states[1]["observed"]), MAX_OBSERVED)
        self.guild.emojis.append(Emoji(999, "restricted", usable=False))
        self.assertNotIn("restricted", self.catalog.prompt(self.guild, "restricted"))
        self.assertNotIn("name219", self.catalog.prompt(self.guild, "name219"))
        self.assertLess(len(await self.catalog.listing(self.guild, 1)), 2000)

    async def test_empty_messages_do_not_touch_database(self):
        with patch.object(self.catalog, "_load", new_callable=AsyncMock) as load:
            await self.catalog.observe(1, "just chatting :nod:")
            load.assert_not_awaited()

    async def test_commands_reject_other_guilds_and_unauthorized_overrides(self):
        bot = SimpleNamespace(emoji_catalog=self.catalog, settings=SimpleNamespace(discord_guild_id=1))
        interaction = SimpleNamespace(guild=self.guild, user=SimpleNamespace(id=3), channel_id=2)
        with patch("nycti.discord.emojis.can_manage_guild", return_value=False):
            result = await manage_emoji(bot, interaction, action="override", emoji="<:nod:456>", alias="test")
        self.assertIn("Manage Server", result)
        self.assertNotIn("test", self.catalog.replacements(self.guild))
        bot.settings.discord_guild_id = 2
        self.assertIn("configured server", await manage_emoji(bot, interaction, action="list"))


class EmojiImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = ActionConfirmationStore()
        self.member = SimpleNamespace(guild_permissions=SimpleNamespace(create_expressions=True))
        self.guild = SimpleNamespace(
            id=1, emojis=[], me=self.member, emoji_limit=50,
            fetch_member=AsyncMock(return_value=self.member),
            fetch_emojis=AsyncMock(return_value=[]),
            get_channel_or_thread=Mock(return_value=SimpleNamespace(
                permissions_for=lambda member: SimpleNamespace(view_channel=True),
            )),
            create_custom_emoji=AsyncMock(return_value=Emoji(456, "frog", True)),
        )
        self.catalog = SimpleNamespace(
            load=AsyncMock(), observe=AsyncMock(), record_import=AsyncMock(),
            imported_id=Mock(return_value=None), resolve=AsyncMock(return_value=ObservedEmoji(123, "frog", True)),
        )
        self.bot = SimpleNamespace(
            get_guild=Mock(return_value=self.guild), user=SimpleNamespace(id=999), emoji_catalog=self.catalog,
            settings=SimpleNamespace(discord_guild_id=1),
        )
        self.executor = ActionToolMixin()
        self.executor.bot = self.bot
        self.executor.action_confirmations = self.store
        self.bot._chat_orchestrator = SimpleNamespace(tool_runner=SimpleNamespace(executor=self.executor))

    async def propose(self):
        return await self.store.propose(
            kind=ActionKind.IMPORT_EMOJI, payload=EmojiImportAction(123, "frog", True),
            guild_id=1, request_channel_id=2, user_id=3, source_message_id=None,
        )

    async def test_command_only_proposes_and_confirmation_uploads_once(self):
        interaction = SimpleNamespace(guild=self.guild, user=SimpleNamespace(id=3,
            guild_permissions=self.member.guild_permissions), channel_id=2)
        card = await manage_emoji(self.bot, interaction, action="import", emoji="<a:frog:123>")
        self.assertIn("Confirmation required", card)
        self.guild.create_custom_emoji.assert_not_awaited()
        proposal_id = card.split("Proposal: `")[1].split("`")[0]
        with patch("nycti.discord.emoji_import.fetch_emoji_image", new=AsyncMock(return_value=b"GIF89a")):
            result = await self.executor.confirm_action(proposal_id, guild_id=1, channel_id=2, user_id=3)
        self.assertIn("Imported", result)
        self.guild.create_custom_emoji.assert_awaited_once()
        self.catalog.record_import.assert_awaited_once_with(1, 123, 456)
        with self.assertRaises(ActionConfirmationError):
            await self.executor.confirm_action(proposal_id, guild_id=1, channel_id=2, user_id=3)

    async def test_permission_revocation_blocks_confirmed_upload(self):
        proposal = await self.propose()
        self.member.guild_permissions.create_expressions = False
        result = await execute_emoji_import(self.bot, proposal)
        self.assertIn("needs Create Expressions", result)
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_duplicate_name_and_full_slots_never_replace(self):
        proposal = await self.propose()
        self.guild.fetch_emojis.return_value = [Emoji(456, "frog")]
        self.assertIn("already exists", await execute_emoji_import(self.bot, proposal))
        self.guild.fetch_emojis.return_value = []
        self.guild.emoji_limit = 0
        self.assertIn("No free", await execute_emoji_import(self.bot, proposal))
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_previously_imported_asset_is_not_uploaded_twice(self):
        self.catalog.imported_id.return_value = 456
        self.guild.fetch_emojis.return_value = [Emoji(456, "frog")]
        self.assertIn("Already available", await execute_emoji_import(self.bot, await self.propose()))
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_invalid_or_cross_user_proposals_rejected(self):
        proposal = await self.propose()
        with self.assertRaises(ActionConfirmationError):
            await self.store.confirm(proposal.proposal_id, guild_id=1, channel_id=2, user_id=4)
        with self.assertRaises(ActionConfirmationError):
            await self.store.propose(kind=ActionKind.IMPORT_EMOJI,
                payload=EmojiImportAction(123, "../../bad", True), guild_id=1, request_channel_id=2,
                user_id=3, source_message_id=None)

    async def test_gif_download_uses_only_the_fixed_discord_cdn(self):
        def handle(request):
            self.assertEqual(request.url.host, "cdn.discordapp.com")
            self.assertEqual(request.url.path, "/emojis/123.gif")
            return httpx.Response(200, content=b"GIF89a")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch("nycti.discord.emoji_import.httpx.AsyncClient", return_value=client):
            self.assertEqual(await fetch_emoji_image(EmojiImportAction(123, "frog", True)), b"GIF89a")

    async def test_download_rejects_oversize_nonimages_and_redirects(self):
        client_type = httpx.AsyncClient
        for status, content in [(200, b"x" * (256 * 1024 + 1)), (200, b"not an image"), (302, b"")]:
            with self.subTest(status=status, length=len(content)):
                transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
                with patch("nycti.discord.emoji_import.httpx.AsyncClient", return_value=client_type(transport=transport)):
                    with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                        await fetch_emoji_image(EmojiImportAction(123, "frog", True))


if __name__ == "__main__":
    unittest.main()
