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
        self.assertEqual(normalize_member_alias("  MemberB  "), "MemberB")
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
            [SimpleNamespace(id=7, alias="MemberB", user_id=123, note="plays ranked")]
        )
        self.assertIn("`7` `MemberB` -> <@123> - plays ranked", rendered)

    def test_member_alias_matches_whole_token_case_insensitive(self) -> None:
        self.assertTrue(member_alias_matches("MemberB", "so what about MemberB?"))
        self.assertFalse(member_alias_matches("MemberB", "MemberB81 was talking"))
        self.assertFalse(member_alias_matches("MemberB", "targets move fast"))

    def test_format_member_reference_block_includes_exact_ping_token(self) -> None:
        rendered = format_member_reference_block(
            [],
            [
                SimpleNamespace(
                    user_id=123,
                    display_name="MemberA",
                    global_name="MemberA.global",
                    username="MemberA_user",
                )
            ],
        )

        self.assertEqual(
            "- MemberA: <@123> (user_id=123; also MemberA.global, MemberA_user)",
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
            name="MemberA_user",
            global_name="MemberA.global",
            display_name="MemberA",
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
                text="Tell MemberA the build is ready.",
            )
            stored = await session.get(MemberIdentity, matches[0].id)

        self.assertEqual([123], [match.user_id for match in matches])
        self.assertEqual("MemberA_user", stored.username)
        self.assertEqual("MemberA.global", stored.global_name)
        self.assertEqual("MemberA", stored.display_name)

    async def test_observed_identity_updates_only_after_name_change(self) -> None:
        service = MemberAliasService()
        member = SimpleNamespace(
            id=123,
            bot=False,
            name="MemberA_user",
            global_name="MemberA.global",
            display_name="MemberA",
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

        member.display_name = "MemberA Prime"
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
                text="ping MemberA Prime",
            )
        self.assertEqual(["MemberA Prime"], [match.display_name for match in matches])

    async def test_matching_references_batches_aliases_and_identities(self) -> None:
        service = MemberAliasService()
        member = SimpleNamespace(
            id=123,
            bot=False,
            name="MemberA_user",
            global_name="MemberA.global",
            display_name="MemberA",
        )
        async with self.sessions() as session:
            await service.set_alias(
                session,
                guild_id=456,
                user_id=789,
                alias="MemberB",
                note="tracks semis",
                created_by_id=123,
            )
            await service.remember_observed_members(
                session,
                guild_id=456,
                members=[member],
            )

            aliases, identities = await service.list_matching_references(
                session,
                guild_id=456,
                text="Tell MemberA and MemberB the market is open.",
            )

        self.assertEqual([(789, "MemberB")], [(item.user_id, item.alias) for item in aliases])
        self.assertEqual(
            [(123, "MemberA")],
            [(item.user_id, item.display_name) for item in identities],
        )


if __name__ == "__main__":
    unittest.main()
