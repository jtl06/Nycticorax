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
        self.assertIsNone(settings.discord_admin_user_id)
        self.assertIsNone(settings.openai_vision_model)
        self.assertIsNone(settings.openai_embedding_model)
        self.assertIsNone(settings.openai_embedding_api_key)
        self.assertIsNone(settings.openai_embedding_base_url)
        self.assertIsNone(settings.twelve_data_api_key)
        self.assertEqual(settings.twelve_data_base_url, "https://api.twelvedata.com")
        self.assertIsNone(settings.openai_base_url)
        self.assertIsNone(settings.tavily_api_key)
        self.assertEqual(settings.reminder_poll_seconds, 60)
        self.assertIsNone(settings.news_channel_id)
        self.assertEqual(settings.news_rss_urls, ())
        self.assertEqual(settings.news_poll_seconds, 300)
        self.assertEqual(settings.news_post_limit_per_poll, 5)
        self.assertTrue(settings.tool_planner_enabled)
        self.assertTrue(settings.tool_answer_rewrite_enabled)
        self.assertEqual(settings.tool_answer_rewrite_min_chars, 260)
        self.assertEqual(settings.profile_update_cooldown_seconds, 1800)
        self.assertFalse(settings.browser_tool_enabled)
        self.assertEqual(settings.browser_tool_timeout_seconds, 20.0)
        self.assertTrue(settings.browser_tool_headless)
        self.assertFalse(settings.browser_tool_allow_headed)
        self.assertTrue(settings.python_tool_enabled)
        self.assertEqual(settings.python_tool_timeout_seconds, 3.0)
        self.assertEqual(settings.python_tool_max_output_chars, 4000)

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

    def test_optional_discord_admin_user_id_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DISCORD_ADMIN_USER_ID": "123456789012345678",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.discord_admin_user_id, 123456789012345678)

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
                "OPENAI_EMBEDDING_MODEL": "text-embedding-3-large",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_embedding_model, "text-embedding-3-large")

    def test_efficiency_model_alias_overrides_memory_model(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_EFFICIENCY_MODEL": "cheap-model",
                "OPENAI_MEMORY_MODEL": "old-memory-model",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_memory_model, "cheap-model")

    def test_optional_embedding_api_key_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "chat-key",
                "OPENAI_EMBEDDING_API_KEY": "embed-key",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_embedding_api_key, "embed-key")

    def test_optional_embedding_base_url_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "chat-key",
                "OPENAI_EMBEDDING_BASE_URL": "https://api.openai.com/v1",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_embedding_base_url, "https://api.openai.com/v1")

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

    def test_optional_twelve_data_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "TWELVE_DATA_API_KEY": "twelve-key",
                "TWELVE_DATA_BASE_URL": "https://api.twelvedata.example.com",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.twelve_data_api_key, "twelve-key")
        self.assertEqual(settings.twelve_data_base_url, "https://api.twelvedata.example.com")

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

    def test_optional_browser_tool_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "BROWSER_TOOL_ENABLED": "true",
                "BROWSER_TOOL_TIMEOUT_SECONDS": "35",
                "BROWSER_TOOL_HEADLESS": "false",
                "BROWSER_TOOL_ALLOW_HEADED": "true",
            }
        )
        self.assertTrue(settings.browser_tool_enabled)
        self.assertEqual(settings.browser_tool_timeout_seconds, 35.0)
        self.assertFalse(settings.browser_tool_headless)
        self.assertTrue(settings.browser_tool_allow_headed)

    def test_optional_python_tool_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "PYTHON_TOOL_ENABLED": "true",
                "PYTHON_TOOL_TIMEOUT_SECONDS": "5",
                "PYTHON_TOOL_MAX_OUTPUT_CHARS": "9000",
            }
        )
        self.assertTrue(settings.python_tool_enabled)
        self.assertEqual(settings.python_tool_timeout_seconds, 5.0)
        self.assertEqual(settings.python_tool_max_output_chars, 9000)

    def test_python_tool_can_be_disabled(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "PYTHON_TOOL_ENABLED": "false",
            }
        )
        self.assertFalse(settings.python_tool_enabled)

    def test_optional_tool_answer_rewrite_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "TOOL_PLANNER_ENABLED": "false",
                "TOOL_ANSWER_REWRITE_ENABLED": "false",
                "TOOL_ANSWER_REWRITE_MIN_CHARS": "420",
                "PROFILE_UPDATE_COOLDOWN_SECONDS": "900",
            }
        )
        self.assertFalse(settings.tool_planner_enabled)
        self.assertFalse(settings.tool_answer_rewrite_enabled)
        self.assertEqual(settings.tool_answer_rewrite_min_chars, 420)
        self.assertEqual(settings.profile_update_cooldown_seconds, 900)

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

    def test_invalid_discord_admin_user_id_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DISCORD_ADMIN_USER_ID": "not-a-number",
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

    def test_invalid_browser_tool_enabled_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "BROWSER_TOOL_ENABLED": "maybe",
                }
            )

    def test_invalid_python_tool_enabled_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "PYTHON_TOOL_ENABLED": "maybe",
                }
            )

    def test_invalid_tool_answer_rewrite_enabled_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "TOOL_ANSWER_REWRITE_ENABLED": "maybe",
                }
            )

    def test_invalid_tool_planner_enabled_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "TOOL_PLANNER_ENABLED": "maybe",
                }
            )

    def test_tool_answer_rewrite_min_chars_out_of_range_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "TOOL_ANSWER_REWRITE_MIN_CHARS": "50",
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

    def test_news_rss_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "NEWS_CHANNEL_ID": "123456789012345678",
                "NEWS_RSS_URLS": "https://example.com/feed.xml, https://example.com/rss",
                "NEWS_POLL_SECONDS": "600",
                "NEWS_POST_LIMIT_PER_POLL": "3",
            }
        )
        self.assertEqual(settings.news_channel_id, 123456789012345678)
        self.assertEqual(
            settings.news_rss_urls,
            ("https://example.com/feed.xml", "https://example.com/rss"),
        )
        self.assertEqual(settings.news_poll_seconds, 600)
        self.assertEqual(settings.news_post_limit_per_poll, 3)

    def test_single_news_rss_url_alias_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "NEWS_CHANNEL_ID": "123456789012345678",
                "NEWS_RSS_URL": "https://example.com/feed.xml",
            }
        )
        self.assertEqual(settings.news_rss_urls, ("https://example.com/feed.xml",))

    def test_news_rss_url_requires_channel_id(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "NEWS_RSS_URL": "https://example.com/feed.xml",
                }
            )

    def test_news_channel_id_can_be_default_for_slash_added_feeds(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "NEWS_CHANNEL_ID": "123456789012345678",
            }
        )
        self.assertEqual(settings.news_channel_id, 123456789012345678)
        self.assertEqual(settings.news_rss_urls, ())

    def test_news_rss_url_requires_http_url(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "NEWS_CHANNEL_ID": "123456789012345678",
                    "NEWS_RSS_URL": "file:///etc/passwd",
                }
            )

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
