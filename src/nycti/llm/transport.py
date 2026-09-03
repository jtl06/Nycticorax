from __future__ import annotations

from typing import Any, Protocol


class LLMTransport(Protocol):
    async def create_chat_completion(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any: ...

    async def create_response(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any: ...


class OpenAISDKTransport:
    """Apply per-request client options without exposing SDK cloning to callers."""

    async def create_chat_completion(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any:
        configured = _configured_client(
            client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        return await configured.chat.completions.create(**request)

    async def create_response(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any:
        configured = _configured_client(
            client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        return await configured.responses.create(**request)


def _configured_client(
    client: Any,
    *,
    timeout_seconds: float | None,
    max_retries: int | None,
) -> Any:
    if not hasattr(client, "with_options"):
        return client
    options: dict[str, object] = {}
    if timeout_seconds is not None:
        options["timeout"] = timeout_seconds
    if max_retries is not None:
        options["max_retries"] = max_retries
    return client.with_options(**options) if options else client
