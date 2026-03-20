import unittest

from nycti.changelog import build_changelog_announcement
from nycti.config import Settings


class ChangelogTests(unittest.TestCase):
    def test_build_changelog_announcement_prefers_markdown_content(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        announcement = build_changelog_announcement(
            settings,
            changelog_reader=lambda: "# Changelog\n\n- shipped reminders",
            commit_sha_reader=lambda: "fcdb209",
        )
        self.assertIsNotNone(announcement)
        assert announcement is not None
        self.assertEqual(announcement.fingerprint, "fcdb209")
        self.assertIn("# Changelog", announcement.content)
        self.assertIn("shipped reminders", announcement.content)

    def test_build_changelog_announcement_can_fall_back_to_git_readers(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        announcement = build_changelog_announcement(
            settings,
            changelog_reader=lambda: None,
            commit_subject_reader=lambda: "fix: startup reminders",
            commit_sha_reader=lambda: "abcd123",
        )
        self.assertIsNotNone(announcement)
        assert announcement is not None
        self.assertEqual(announcement.fingerprint, "abcd123")
        self.assertIn("fix: startup reminders", announcement.content)

    def test_build_changelog_announcement_returns_none_without_message_or_git_fallback(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertIsNone(
            build_changelog_announcement(
                settings,
                changelog_reader=lambda: None,
                commit_subject_reader=lambda: None,
                commit_sha_reader=lambda: None,
            )
        )


if __name__ == "__main__":
    unittest.main()
