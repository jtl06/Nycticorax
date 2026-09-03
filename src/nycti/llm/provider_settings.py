from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FallbackProviderSettings:
    openai_api_key: str
    openai_base_url: str
    openai_chat_model: str
    openai_embedding_api_key: str | None = None
    openai_embedding_base_url: str | None = None
    openai_chat_model_fallbacks: tuple[str, ...] = ()
    openai_memory_model: str = ""
    openai_reasoning_effort: str | None = None
    openai_efficiency_reasoning_effort: str | None = None
    openai_fallback_api_key: str | None = None
    openai_fallback_base_url: str | None = None
    openai_fallback_chat_model: str | None = None
