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
        self.assertEqual(settings.openai_chat_model_fallbacks, ())
        self.assertIsNone(settings.openai_vision_model)
        self.assertIsNone(settings.openai_embedding_model)
        self.assertIsNone(settings.openai_base_url)
        self.assertIsNone(settings.tavily_api_key)
        self.assertEqual(settings.reminder_poll_seconds, 60)

    def test_optional_vision_model_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_VISION_MODEL": "gpt-4.1-mini-vision",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_vision_model, "gpt-4.1-mini-vision")

    def test_optional_chat_model_fallbacks_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_CHAT_MODEL_FALLBACKS": "backup-a, backup-b , backup-c",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_chat_model_fallbacks, ("backup-a", "backup-b", "backup-c"))

    def test_optional_embedding_model_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_EMBEDDING_MODEL": "https://clarifai.com/openai/embed/models/text-embedding-3-large",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(
            settings.openai_embedding_model,
            "https://clarifai.com/openai/embed/models/text-embedding-3-large",
        )

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

    def test_reminder_poll_seconds_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "REMINDER_POLL_SECONDS": "120",
            }
        )
        self.assertEqual(settings.reminder_poll_seconds, 120)

    def test_reminder_poll_seconds_below_limit_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "REMINDER_POLL_SECONDS": "29",
                }
            )


if __name__ == "__main__":
    unittest.main()
