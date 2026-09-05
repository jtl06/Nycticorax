from __future__ import annotations

from dataclasses import replace
import re

from nycti.llm.provider_policy import ProviderErrorKind, classify_provider_error
from nycti.llm.types import LLMProviderAttempt


def should_fail_over_chat_model(exc: Exception) -> bool:
    return classify_provider_error(exc) in {
        ProviderErrorKind.DEPLOYMENT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.ACCESS_DENIED,
        ProviderErrorKind.TRANSIENT,
    }


def is_deterministic_model_unavailable_error(exc: Exception) -> bool:
    return classify_provider_error(exc) == ProviderErrorKind.DEPLOYMENT


def should_retry_busy_foreground_chat(feature: str, exc: Exception) -> bool:
    return feature.startswith("chat_reply") and classify_provider_error(exc) == ProviderErrorKind.RATE_LIMIT


def is_transient_provider_error(exc: Exception) -> bool:
    return classify_provider_error(exc) in {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TRANSIENT,
    }


def provider_label(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    return normalized.rstrip("/") if normalized else "openai-default"


def offset_provider_attempts(
    attempts: object,
    *,
    offset: int,
) -> list[LLMProviderAttempt]:
    if not isinstance(attempts, list):
        return []
    return [
        replace(attempt, attempt=attempt.attempt + offset)
        for attempt in attempts
        if isinstance(attempt, LLMProviderAttempt)
    ]


def record_last_parse_timing(
    attempts: list[LLMProviderAttempt],
    parse_ms: int,
) -> None:
    if attempts:
        attempts[-1] = replace(attempts[-1], parse_ms=max(parse_ms, 0))


def summarize_provider_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"
