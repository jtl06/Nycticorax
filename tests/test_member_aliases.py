import unittest
from types import SimpleNamespace

from nycti.member_aliases import (
    format_member_alias_list,
    member_alias_matches,
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


if __name__ == "__main__":
    unittest.main()
