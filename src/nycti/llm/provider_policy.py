from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class ProviderErrorKind(StrEnum):
    TOOL_INCOMPATIBLE = "tool_incompatible"
    AUTHENTICATION = "authentication"
    DEPLOYMENT = "deployment"
    RATE_LIMIT = "rate_limit"
    ACCESS_DENIED = "access_denied"
    TRANSIENT = "transient"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    name: str
    label: str
    native_tools: bool
    vision: bool
    text_token_fields: tuple[str, ...]
    image_token_fields: tuple[str, ...]
    request_timeout_seconds: float
    request_max_retries: int

    def token_fields(self, *, has_images: bool) -> tuple[str, ...]:
        return self.image_token_fields if has_images else self.text_token_fields


def failover_cooldown_seconds(error_kind: ProviderErrorKind) -> float:
    return {
        ProviderErrorKind.DEPLOYMENT: 900.0,
        ProviderErrorKind.ACCESS_DENIED: 300.0,
        ProviderErrorKind.RATE_LIMIT: 20.0,
        ProviderErrorKind.TRANSIENT: 60.0,
    }.get(error_kind, 0.0)


def capabilities_for_base_url(base_url: str | None) -> ProviderCapabilities:
    normalized = str(base_url or "").strip().rstrip("/")
    hostname = (urlparse(normalized).hostname or "").casefold()
    label = normalized or "openai-default"
    if "clarifai.com" in hostname:
        return ProviderCapabilities(
            name="clarifai",
            label=label,
            native_tools=True,
            vision=True,
            text_token_fields=("max_tokens",),
            image_token_fields=("max_completion_tokens", "max_tokens", ""),
            request_timeout_seconds=35,
            request_max_retries=0,
        )
    if not normalized or "api.openai.com" in hostname:
        return ProviderCapabilities(
            name="openai",
            label=label,
            native_tools=True,
            vision=True,
            text_token_fields=("max_completion_tokens", "max_tokens"),
            image_token_fields=("max_completion_tokens",),
            request_timeout_seconds=30,
            request_max_retries=1,
        )
    return ProviderCapabilities(
        name=hostname or "openai-compatible",
        label=label,
        native_tools=True,
        vision=True,
        text_token_fields=("max_tokens",),
        image_token_fields=("max_completion_tokens", "max_tokens", ""),
        request_timeout_seconds=30,
        request_max_retries=0,
    )


def classify_provider_error(exc: Exception) -> ProviderErrorKind:
    normalized = str(exc).casefold()
    if any(
        signal in normalized
        for signal in (
            "unsupported tool",
            "tools are not supported",
            "tool use is not supported",
            "invalid tool schema",
            "tool schema",
            "tool_choice is not supported",
        )
    ):
        return ProviderErrorKind.TOOL_INCOMPATIBLE
    if any(
        signal in normalized
        for signal in (
            "error code: 401",
            "status code: 401",
            "authenticationerror",
            "invalid api key",
            "incorrect api key",
            "missing api key",
            "unauthorized",
        )
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(
        signal in normalized
        for signal in (
            "invalid model",
            "unknown model",
            "unsupported model",
            "model not found",
            "no such model",
            "does not exist",
            "error code: 404",
            "status code: 404",
            "model prediction failed",
            "requires a dedicated deployment",
            "no deployed version was found",
            "restricted to shared compute",
            "dedicated nodepool",
        )
    ):
        return ProviderErrorKind.DEPLOYMENT
    if any(
        signal in normalized
        for signal in (
            "error code: 429",
            "status code: 429",
            "ratelimiterror",
            "rate limit",
            "too many requests",
            "model is busy",
            "busy processing",
        )
    ):
        return ProviderErrorKind.RATE_LIMIT
    if any(
        signal in normalized
        for signal in (
            "error code: 403",
            "status code: 403",
            "403 forbidden",
            "permissiondeniederror",
            "permission denied",
            "access denied",
            "forbidden",
        )
    ):
        return ProviderErrorKind.ACCESS_DENIED
    if any(
        signal in normalized
        for signal in (
            "took too long",
            "timeout",
            "timed out",
            "connection error",
            "temporarily unavailable",
            "service unavailable",
            "internal error",
        )
    ):
        return ProviderErrorKind.TRANSIENT
    if any(
        signal in normalized
        for signal in (
            "bad request",
            "invalid request",
            "unprocessable",
            "error code: 400",
            "status code: 400",
            "error code: 422",
            "status code: 422",
        )
    ):
        return ProviderErrorKind.INVALID_REQUEST
    return ProviderErrorKind.UNKNOWN
