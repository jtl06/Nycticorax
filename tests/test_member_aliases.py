import unittest
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.db.models import Base, MemberIdentity
from nycti.member_aliases import (
    format_member_alias_list,
    format_member_reference_block,
    member_alias_matches,
    MemberAliasService,
    normalize_member_alias,
    normalize_member_note,
)


class MemberAliasTests(unittest.TestCase):
    def test_normalize_member_alias_accepts_short_aliases(self) -> None:
        self.assertEqual(normalize_member_alias("  GTS  "), "GTS")
        self.assertEqual(normalize_member_alias("great top scorer"), "great top scorer")

    def test_normalize_member_alias_rejects_bad_values(self) -> None:
        self.assertIsNone(normalize_member_alias(""))
        self.assertIsNone(normalize_member_alias("bad/alias"))
        self.assertIsNone(normalize_member_alias("x" * 41))

    def test_normalize_member_note_collapses_and_caps(self) -> None:
        note = normalize_member_note("  plays   ranked\nusually  ")
        self.assertEqual(note, "plays ranked usually")
        self.assertLessEqual(len(normalize_member_note("x" * 300)), 160)

    def test_format_member_alias_list(self) -> None:
        rendered = format_member_alias_list(
            [SimpleNamespace(id=7, alias="GTS", user_id=123, note="plays ranked")]
        )
        self.assertIn("`7` `GTS` -> <@123> - plays ranked", rendered)

    def test_member_alias_matches_whole_token_case_insensitive(self) -> None:
        self.assertTrue(member_alias_matches("GTS", "so what about gts?"))
        self.assertFalse(member_alias_matches("GTS", "gts81 was talking"))
        self.assertFalse(member_alias_matches("GTS", "targets move fast"))

    def test_format_member_reference_block_includes_exact_ping_token(self) -> None:
        rendered = format_member_reference_block(
            [],
            [
                SimpleNamespace(
                    user_id=123,
                    display_name="Lucis",
                    global_name="lucis.global",
                    username="lucis_user",
                )
            ],
        )

        self.assertEqual(
            "- Lucis: <@123> (user_id=123; also lucis.global, lucis_user)",
            rendered,
        )


class MemberIdentityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_observed_identity_is_retained_and_matches_names(self) -> None:
        service = MemberAliasService()
        member = SimpleNamespace(
            id=123,
            bot=False,
            name="lucis_user",
            global_name="lucis.global",
            display_name="Lucis",
        )

        async with self.sessions() as session:
            changed = await service.remember_observed_members(
                session,
                guild_id=456,
                members=[member],
            )
            await session.commit()

        self.assertEqual(1, changed)
        self.assertFalse(service.needs_identity_update(guild_id=456, member=member))

        async with self.sessions() as session:
            matches = await service.list_matching_identities(
                session,
                guild_id=456,
                text="Tell Lucis the build is ready.",
            )
            stored = await session.get(MemberIdentity, matches[0].id)

        self.assertEqual([123], [match.user_id for match in matches])
        self.assertEqual("lucis_user", stored.username)
        self.assertEqual("lucis.global", stored.global_name)
        self.assertEqual("Lucis", stored.display_name)

    async def test_observed_identity_updates_only_after_name_change(self) -> None:
        service = MemberAliasService()
        member = SimpleNamespace(
            id=123,
            bot=False,
            name="lucis_user",
            global_name="lucis.global",
            display_name="Lucis",
        )
        async with self.sessions() as session:
            self.assertEqual(
                1,
                await service.remember_observed_members(
                    session,
                    guild_id=456,
                    members=[member],
                ),
            )
            await session.commit()

        async with self.sessions() as session:
            self.assertEqual(
                0,
                await service.remember_observed_members(
                    session,
                    guild_id=456,
                    members=[member],
                ),
            )

        member.display_name = "Lucis Prime"
        async with self.sessions() as session:
            self.assertEqual(
                1,
                await service.remember_observed_members(
                    session,
                    guild_id=456,
                    members=[member],
                ),
            )
            await session.commit()

        async with self.sessions() as session:
            matches = await service.list_matching_identities(
                session,
                guild_id=456,
                text="ping Lucis Prime",
            )
        self.assertEqual(["Lucis Prime"], [match.display_name for match in matches])


if __name__ == "__main__":
    unittest.main()
