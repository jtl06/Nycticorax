from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import time
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

    def __init__(self) -> None:
        self._timing: ContextVar[TransportTiming] = ContextVar(
            "nycti_transport_timing",
            default=TransportTiming(),
        )

    async def create_chat_completion(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any:
        return await self._run(
            client=client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            call=lambda configured: configured.chat.completions.create(**request),
        )

    async def create_response(
        self,
        *,
        client: Any,
        request: dict[str, object],
        timeout_seconds: float | None,
        max_retries: int | None,
    ) -> Any:
        return await self._run(
            client=client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            call=lambda configured: configured.responses.create(**request),
        )

    def timing(self) -> TransportTiming:
        return self._timing.get()

    async def _run(
        self,
        *,
        client: Any,
        timeout_seconds: float | None,
        max_retries: int | None,
        call,
    ) -> Any:  # type: ignore[no-untyped-def]
        self._timing.set(TransportTiming())
        configured = _configured_client(
            client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        request_started_at = time.perf_counter()
        try:
            return await call(configured)
        finally:
            self._timing.set(
                TransportTiming(
                    request_ms=max(round((time.perf_counter() - request_started_at) * 1000), 0),
                )
            )


@dataclass(frozen=True, slots=True)
class TransportTiming:
    request_ms: int = 0


def transport_timing(transport: object) -> TransportTiming:
    getter = getattr(transport, "timing", None)
    if not callable(getter):
        return TransportTiming()
    value = getter()
    return value if isinstance(value, TransportTiming) else TransportTiming()


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
