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
        self.assertIsNone(settings.openai_quick_model)
        self.assertIsNone(settings.openai_deep_model)
        self.assertEqual(settings.openai_chat_model_fallbacks, ())
        self.assertIsNone(settings.openai_reasoning_effort)
        self.assertIsNone(settings.openai_efficiency_reasoning_effort)
        self.assertIsNone(settings.discord_admin_user_id)
        self.assertIsNone(settings.openai_vision_model)
        self.assertIsNone(settings.openai_embedding_model)
        self.assertIsNone(settings.openai_embedding_api_key)
        self.assertIsNone(settings.openai_embedding_base_url)
        self.assertIsNone(settings.twelve_data_api_key)
        self.assertEqual(settings.twelve_data_base_url, "https://api.twelvedata.com")
        self.assertIsNone(settings.openai_base_url)
        self.assertIsNone(settings.openai_fallback_api_key)
        self.assertIsNone(settings.openai_fallback_base_url)
        self.assertIsNone(settings.openai_fallback_chat_model)
        self.assertIsNone(settings.tavily_api_key)
        self.assertEqual(settings.tavily_search_depth, "ultra-fast")
        self.assertIsNone(settings.error_debug_channel_id)
        self.assertEqual(settings.discord_invocation_modes, ("mention_reply",))
        self.assertEqual(settings.discord_invocation_name, "Nycti")
        self.assertEqual(settings.discord_ambient_channel_ids, ())
        self.assertEqual(settings.discord_ambient_cooldown_seconds, 30)
        self.assertFalse(settings.persist_bad_bot_diagnostics)
        self.assertEqual(settings.reminder_poll_seconds, 60)
        self.assertEqual(settings.profile_update_cooldown_seconds, 1800)
        self.assertFalse(settings.browser_tool_enabled)
        self.assertEqual(settings.browser_tool_timeout_seconds, 20.0)
        self.assertTrue(settings.browser_tool_headless)
        self.assertFalse(settings.browser_tool_allow_headed)
        self.assertTrue(settings.python_tool_enabled)
        self.assertEqual(settings.python_tool_timeout_seconds, 3.0)
        self.assertEqual(settings.python_tool_max_output_chars, 4000)
        self.assertTrue(settings.youtube_transcript_enabled)
        self.assertEqual(settings.youtube_transcript_timeout_seconds, 10.0)
        self.assertEqual(settings.youtube_transcript_max_chars, 6000)

    def test_optional_discord_invocation_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "DISCORD_INVOCATION_MODES": "mention-reply, explicit_name, ambient",
                "DISCORD_INVOCATION_NAME": "Owly",
                "DISCORD_AMBIENT_CHANNEL_IDS": "123, 456, 123",
                "DISCORD_AMBIENT_COOLDOWN_SECONDS": "45",
            }
        )

        self.assertEqual(
            settings.discord_invocation_modes,
            ("mention_reply", "explicit_name", "ambient"),
        )
        self.assertEqual(settings.discord_invocation_name, "Owly")
        self.assertEqual(settings.discord_ambient_channel_ids, (123, 456))
        self.assertEqual(settings.discord_ambient_cooldown_seconds, 45)

    def test_persistent_bad_bot_diagnostics_are_explicit_opt_in(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "PERSIST_BAD_BOT_DIAGNOSTICS": "true",
            }
        )

        self.assertTrue(settings.persist_bad_bot_diagnostics)

    def test_ambient_invocation_requires_allowlisted_channels(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_AMBIENT_CHANNEL_IDS"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "DISCORD_INVOCATION_MODES": "ambient",
                }
            )

    def test_rejects_unknown_discord_invocation_mode(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_INVOCATION_MODES"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "DISCORD_INVOCATION_MODES": "always_listen",
                }
            )

    def test_rejects_invalid_ambient_channel_ids_and_cooldown(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "comma-separated"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "DISCORD_AMBIENT_CHANNEL_IDS": "not-an-id",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_AMBIENT_COOLDOWN_SECONDS"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "DISCORD_AMBIENT_COOLDOWN_SECONDS": "0",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "positive integers"):
            Settings(
                discord_token="discord-token",
                openai_api_key="openai-key",
                database_url="sqlite:///tmp.db",
                discord_ambient_channel_ids=(-1,),
            )

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

    def test_optional_error_debug_channel_id_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "ERROR_DEBUG_CHANNEL_ID": "1505623876669931642",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.error_debug_channel_id, 1505623876669931642)

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

    def test_optional_answer_profile_models_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "OPENAI_QUICK_MODEL": "fast-model",
                "OPENAI_DEEP_MODEL": "rigorous-model",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )

        self.assertEqual("fast-model", settings.openai_quick_model)
        self.assertEqual("rigorous-model", settings.openai_deep_model)

    def test_reasoning_efforts_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "official-openai-key",
                "OPENAI_REASONING_EFFORT": "HIGH",
                "OPENAI_EFFICIENCY_REASONING_EFFORT": "minimal",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_reasoning_effort, "high")
        self.assertEqual(settings.openai_efficiency_reasoning_effort, "minimal")

    def test_rejects_unsupported_reasoning_effort(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "OPENAI_REASONING_EFFORT"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "OPENAI_REASONING_EFFORT": "extreme",
                    "DATABASE_URL": "sqlite:///tmp.db",
                }
            )

    def test_optional_cross_provider_fallback_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "primary-key",
                "OPENAI_FALLBACK_API_KEY": "fallback-key",
                "OPENAI_FALLBACK_BASE_URL": "https://api.deepinfra.com/v1/openai",
                "OPENAI_FALLBACK_CHAT_MODEL": "moonshotai/Kimi-K2.5",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.openai_fallback_api_key, "fallback-key")
        self.assertEqual(
            settings.openai_fallback_base_url,
            "https://api.deepinfra.com/v1/openai",
        )
        self.assertEqual(settings.openai_fallback_chat_model, "moonshotai/Kimi-K2.5")

    def test_cross_provider_fallback_requires_all_settings(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be configured together"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "primary-key",
                    "OPENAI_FALLBACK_BASE_URL": "https://api.deepinfra.com/v1/openai",
                    "OPENAI_FALLBACK_CHAT_MODEL": "moonshotai/Kimi-K2.5",
                    "DATABASE_URL": "sqlite:///tmp.db",
                }
            )

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

    def test_optional_tavily_search_depth_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "TAVILY_SEARCH_DEPTH": "fast",
                "DATABASE_URL": "sqlite:///tmp.db",
            }
        )
        self.assertEqual(settings.tavily_search_depth, "fast")

    def test_invalid_tavily_search_depth_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "TAVILY_SEARCH_DEPTH": "turbo",
                    "DATABASE_URL": "sqlite:///tmp.db",
                }
            )

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

    def test_optional_youtube_transcript_settings_load(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "YOUTUBE_TRANSCRIPT_ENABLED": "false",
                "YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS": "15",
                "YOUTUBE_TRANSCRIPT_MAX_CHARS": "12000",
            }
        )
        self.assertFalse(settings.youtube_transcript_enabled)
        self.assertEqual(settings.youtube_transcript_timeout_seconds, 15.0)
        self.assertEqual(settings.youtube_transcript_max_chars, 12000)

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

    def test_optional_profile_update_cooldown_loads(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "PROFILE_UPDATE_COOLDOWN_SECONDS": "900",
            }
        )
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

    def test_invalid_youtube_transcript_enabled_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "OPENAI_API_KEY": "openai-key",
                    "DATABASE_URL": "sqlite:///tmp.db",
                    "YOUTUBE_TRANSCRIPT_ENABLED": "maybe",
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

    def test_max_completion_tokens_above_limit_clamps_from_env(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "MAX_COMPLETION_TOKENS": "8193",
            }
        )
        self.assertEqual(settings.max_completion_tokens, 8192)

    def test_max_completion_tokens_below_limit_clamps_from_env(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "OPENAI_API_KEY": "openai-key",
                "DATABASE_URL": "sqlite:///tmp.db",
                "MAX_COMPLETION_TOKENS": "12",
            }
        )
        self.assertEqual(settings.max_completion_tokens, 64)

    def test_max_completion_tokens_direct_constructor_remains_strict(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings(
                discord_token="discord-token",
                openai_api_key="openai-key",
                database_url="sqlite:///tmp.db",
                max_completion_tokens=8193,
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
