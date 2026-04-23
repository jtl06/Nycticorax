from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during bare test runs
    load_dotenv = None


class ConfigurationError(ValueError):
    pass


def _load_dotenv() -> None:
    if load_dotenv is not None:
        load_dotenv()


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {key}")
    return value


def _parse_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer.") from exc


def _parse_optional_int(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer.") from exc


def _parse_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a float.") from exc


def _parse_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be a boolean (true/false).")


def _parse_csv(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = env.get(key, "")
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_news_rss_urls(env: Mapping[str, str]) -> tuple[str, ...]:
    urls = [*list(_parse_csv(env, "NEWS_RSS_URLS"))]
    single_url = env.get("NEWS_RSS_URL", "").strip()
    if single_url:
        urls.append(single_url)
    return tuple(dict.fromkeys(urls))


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


@dataclass(slots=True)
class Settings:
    discord_token: str
    openai_api_key: str
    database_url: str
    openai_base_url: str | None = None
    openai_embedding_api_key: str | None = None
    openai_embedding_base_url: str | None = None
    twelve_data_api_key: str | None = None
    twelve_data_base_url: str = "https://api.twelvedata.com"
    tavily_api_key: str | None = None
    discord_guild_id: int | None = None
    discord_admin_user_id: int | None = None
    openai_chat_model: str = "gpt-4.1-mini"
    openai_chat_model_fallbacks: tuple[str, ...] = ()
    openai_memory_model: str = "gpt-4.1-nano"
    openai_vision_model: str | None = None
    openai_embedding_model: str | None = None
    memory_confidence_threshold: float = 0.78
    channel_context_limit: int = 12
    memory_retrieval_limit: int = 4
    max_completion_tokens: int = 350
    tool_answer_rewrite_enabled: bool = True
    tool_answer_rewrite_min_chars: int = 260
    profile_update_cooldown_seconds: int = 1800
    reminder_poll_seconds: int = 60
    news_channel_id: int | None = None
    news_rss_urls: tuple[str, ...] = ()
    news_poll_seconds: int = 300
    news_post_limit_per_poll: int = 5
    browser_tool_enabled: bool = False
    browser_tool_timeout_seconds: float = 20.0
    browser_tool_headless: bool = True
    browser_tool_allow_headed: bool = False

    def __post_init__(self) -> None:
        if self.memory_confidence_threshold <= 0 or self.memory_confidence_threshold > 1:
            raise ConfigurationError("MEMORY_CONFIDENCE_THRESHOLD must be between 0 and 1.")
        if self.channel_context_limit < 3 or self.channel_context_limit > 20:
            raise ConfigurationError("CHANNEL_CONTEXT_LIMIT must be between 3 and 20.")
        if self.memory_retrieval_limit < 1 or self.memory_retrieval_limit > 10:
            raise ConfigurationError("MEMORY_RETRIEVAL_LIMIT must be between 1 and 10.")
        if self.max_completion_tokens < 64 or self.max_completion_tokens > 8192:
            raise ConfigurationError("MAX_COMPLETION_TOKENS must be between 64 and 8192.")
        if self.tool_answer_rewrite_min_chars < 80 or self.tool_answer_rewrite_min_chars > 2000:
            raise ConfigurationError("TOOL_ANSWER_REWRITE_MIN_CHARS must be between 80 and 2000.")
        if self.profile_update_cooldown_seconds < 0 or self.profile_update_cooldown_seconds > 86400:
            raise ConfigurationError("PROFILE_UPDATE_COOLDOWN_SECONDS must be between 0 and 86400.")
        if self.reminder_poll_seconds < 30 or self.reminder_poll_seconds > 300:
            raise ConfigurationError("REMINDER_POLL_SECONDS must be between 30 and 300.")
        if self.news_poll_seconds < 60 or self.news_poll_seconds > 3600:
            raise ConfigurationError("NEWS_POLL_SECONDS must be between 60 and 3600.")
        if self.news_post_limit_per_poll < 1 or self.news_post_limit_per_poll > 10:
            raise ConfigurationError("NEWS_POST_LIMIT_PER_POLL must be between 1 and 10.")
        if self.browser_tool_timeout_seconds < 5 or self.browser_tool_timeout_seconds > 120:
            raise ConfigurationError("BROWSER_TOOL_TIMEOUT_SECONDS must be between 5 and 120.")
        if self.news_rss_urls and self.news_channel_id is None:
            raise ConfigurationError("NEWS_CHANNEL_ID is required when NEWS_RSS_URLS or NEWS_RSS_URL is set.")
        if any(not url.startswith(("https://", "http://")) for url in self.news_rss_urls):
            raise ConfigurationError("NEWS_RSS_URLS entries must start with http:// or https://.")
        supported_prefixes = (
            "postgresql+psycopg://",
            "sqlite+aiosqlite:///",
            "sqlite:///",
        )
        if not self.database_url.startswith(supported_prefixes):
            raise ConfigurationError(
                "DATABASE_URL must start with postgresql+psycopg://, postgresql://, postgres://, or sqlite:///."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        _load_dotenv()
        source = os.environ if env is None else env
        guild_id = source.get("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            try:
                parsed_guild_id = int(guild_id)
            except ValueError as exc:
                raise ConfigurationError("DISCORD_GUILD_ID must be an integer.") from exc
        else:
            parsed_guild_id = None
        return cls(
            discord_token=_require(source, "DISCORD_TOKEN"),
            openai_api_key=_require(source, "OPENAI_API_KEY"),
            database_url=_normalize_database_url(_require(source, "DATABASE_URL")),
            openai_base_url=source.get("OPENAI_BASE_URL", "").strip() or None,
            openai_embedding_api_key=source.get("OPENAI_EMBEDDING_API_KEY", "").strip() or None,
            openai_embedding_base_url=source.get("OPENAI_EMBEDDING_BASE_URL", "").strip() or None,
            twelve_data_api_key=source.get("TWELVE_DATA_API_KEY", "").strip() or None,
            twelve_data_base_url=source.get("TWELVE_DATA_BASE_URL", "https://api.twelvedata.com").strip() or "https://api.twelvedata.com",
            tavily_api_key=source.get("TAVILY_API_KEY", "").strip() or None,
            discord_guild_id=parsed_guild_id,
            discord_admin_user_id=_parse_optional_int(source, "DISCORD_ADMIN_USER_ID"),
            openai_chat_model=source.get("OPENAI_CHAT_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
            openai_chat_model_fallbacks=_parse_csv(source, "OPENAI_CHAT_MODEL_FALLBACKS"),
            openai_memory_model=(
                source.get("OPENAI_EFFICIENCY_MODEL", "").strip()
                or source.get("OPENAI_MEMORY_MODEL", "gpt-4.1-nano").strip()
                or "gpt-4.1-nano"
            ),
            openai_vision_model=source.get("OPENAI_VISION_MODEL", "").strip() or None,
            openai_embedding_model=source.get("OPENAI_EMBEDDING_MODEL", "").strip() or None,
            memory_confidence_threshold=_parse_float(
                source, "MEMORY_CONFIDENCE_THRESHOLD", 0.78
            ),
            channel_context_limit=_parse_int(source, "CHANNEL_CONTEXT_LIMIT", 12),
            memory_retrieval_limit=_parse_int(source, "MEMORY_RETRIEVAL_LIMIT", 4),
            max_completion_tokens=_parse_int(source, "MAX_COMPLETION_TOKENS", 350),
            tool_answer_rewrite_enabled=_parse_bool(source, "TOOL_ANSWER_REWRITE_ENABLED", True),
            tool_answer_rewrite_min_chars=_parse_int(source, "TOOL_ANSWER_REWRITE_MIN_CHARS", 260),
            profile_update_cooldown_seconds=_parse_int(source, "PROFILE_UPDATE_COOLDOWN_SECONDS", 1800),
            reminder_poll_seconds=_parse_int(source, "REMINDER_POLL_SECONDS", 60),
            news_channel_id=_parse_optional_int(source, "NEWS_CHANNEL_ID"),
            news_rss_urls=_parse_news_rss_urls(source),
            news_poll_seconds=_parse_int(source, "NEWS_POLL_SECONDS", 300),
            news_post_limit_per_poll=_parse_int(source, "NEWS_POST_LIMIT_PER_POLL", 5),
            browser_tool_enabled=_parse_bool(source, "BROWSER_TOOL_ENABLED", False),
            browser_tool_timeout_seconds=_parse_float(source, "BROWSER_TOOL_TIMEOUT_SECONDS", 20.0),
            browser_tool_headless=_parse_bool(source, "BROWSER_TOOL_HEADLESS", True),
            browser_tool_allow_headed=_parse_bool(source, "BROWSER_TOOL_ALLOW_HEADED", False),
        )
