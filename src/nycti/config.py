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


def _parse_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a float.") from exc


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
    tavily_api_key: str | None = None
    discord_guild_id: int | None = None
    openai_chat_model: str = "gpt-4.1-mini"
    openai_memory_model: str = "gpt-4.1-nano"
    memory_confidence_threshold: float = 0.78
    channel_context_limit: int = 12
    memory_retrieval_limit: int = 4
    max_completion_tokens: int = 350

    def __post_init__(self) -> None:
        if self.memory_confidence_threshold <= 0 or self.memory_confidence_threshold > 1:
            raise ConfigurationError("MEMORY_CONFIDENCE_THRESHOLD must be between 0 and 1.")
        if self.channel_context_limit < 3 or self.channel_context_limit > 20:
            raise ConfigurationError("CHANNEL_CONTEXT_LIMIT must be between 3 and 20.")
        if self.memory_retrieval_limit < 1 or self.memory_retrieval_limit > 10:
            raise ConfigurationError("MEMORY_RETRIEVAL_LIMIT must be between 1 and 10.")
        if self.max_completion_tokens < 64 or self.max_completion_tokens > 8192:
            raise ConfigurationError("MAX_COMPLETION_TOKENS must be between 64 and 8192.")

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
            tavily_api_key=source.get("TAVILY_API_KEY", "").strip() or None,
            discord_guild_id=parsed_guild_id,
            openai_chat_model=source.get("OPENAI_CHAT_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
            openai_memory_model=source.get("OPENAI_MEMORY_MODEL", "gpt-4.1-nano").strip() or "gpt-4.1-nano",
            memory_confidence_threshold=_parse_float(
                source, "MEMORY_CONFIDENCE_THRESHOLD", 0.78
            ),
            channel_context_limit=_parse_int(source, "CHANNEL_CONTEXT_LIMIT", 12),
            memory_retrieval_limit=_parse_int(source, "MEMORY_RETRIEVAL_LIMIT", 4),
            max_completion_tokens=_parse_int(source, "MAX_COMPLETION_TOKENS", 350),
        )
