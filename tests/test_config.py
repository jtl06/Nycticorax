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
        self.assertIsNone(settings.tavily_api_key)

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

    def test_optional_tavily_api_key_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "TAVILY_API_KEY": "tvly-test-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.tavily_api_key, "tvly-test-key")

    def test_postgresql_url_is_normalized_to_psycopg(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "postgresql://user:pass@host:5432/dbname",
            }
        )
        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://user:pass@host:5432/dbname",
        )

    def test_postgres_url_is_normalized_to_psycopg(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "postgres://user:pass@host:5432/dbname",
            }
        )
        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://user:pass@host:5432/dbname",
        )

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

    def test_invalid_database_scheme_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "mysql://user:pass@host:3306/dbname",
                }
            )

    def test_max_completion_tokens_allows_8192(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "MAX_COMPLETION_TOKENS": "8192",
            }
        )
        self.assertEqual(settings.max_completion_tokens, 8192)

    def test_max_completion_tokens_above_limit_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "MAX_COMPLETION_TOKENS": "8193",
                }
            )


if __name__ == "__main__":
    unittest.main()
