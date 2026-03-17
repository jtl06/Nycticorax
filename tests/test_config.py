import unittest

from nycti.config import ConfigurationError, Settings


class ConfigValidationTests(unittest.TestCase):
    def test_valid_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.channel_context_limit, 12)
        self.assertEqual(settings.openai_chat_model, "gpt-4.1-mini")
        self.assertIsNone(settings.openai_base_url)

    def test_optional_base_url_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_BASE_URL": "https://api.sambanova.ai/v1",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_base_url, "https://api.sambanova.ai/v1")

    def test_missing_required_value_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                }
            )

    def test_invalid_threshold_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "MEMORY_CONFIDENCE_THRESHOLD": "1.5",
                }
            )


if __name__ == "__main__":
    unittest.main()
