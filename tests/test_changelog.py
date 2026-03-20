import unittest

from nycti.changelog import build_changelog_announcement
from nycti.config import Settings


class ChangelogTests(unittest.TestCase):
    def test_build_changelog_announcement_uses_explicit_settings(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "CHANGELOG_CHANNEL_ID": "123",
                "CHANGELOG_MESSAGE": "feat: shipped reminders",
                "CHANGELOG_VERSION": "fcdb209",
            }
        )
        announcement = build_changelog_announcement(settings)
        self.assertIsNotNone(announcement)
        assert announcement is not None
        self.assertEqual(announcement.channel_id, 123)
        self.assertEqual(announcement.fingerprint, "fcdb209")
        self.assertIn("changelog: feat: shipped reminders", announcement.content)
        self.assertIn("version: `fcdb209`", announcement.content)

    def test_build_changelog_announcement_can_fall_back_to_git_readers(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "CHANGELOG_CHANNEL_ID": "123",
            }
        )
        announcement = build_changelog_announcement(
            settings,
            commit_subject_reader=lambda: "fix: startup reminders",
            commit_sha_reader=lambda: "abcd123",
        )
        self.assertIsNotNone(announcement)
        assert announcement is not None
        self.assertEqual(announcement.fingerprint, "abcd123")
        self.assertIn("fix: startup reminders", announcement.content)

    def test_build_changelog_announcement_returns_none_without_channel(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertIsNone(build_changelog_announcement(settings))


if __name__ == "__main__":
    unittest.main()
