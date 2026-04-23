from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Mapping

LOGGER = logging.getLogger(__name__)
MAX_TRACE_VALUE_CHARS = 80


@dataclass(slots=True)
class AgentSpan:
    name: str
    elapsed_ms: int
    attrs: dict[str, str] = field(default_factory=dict)


class AgentTrace:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._spans: list[AgentSpan] = []

    def mark(
        self,
        name: str,
        *,
        started_at: float,
        attrs: Mapping[str, object | None] | None = None,
    ) -> None:
        self.add(name, elapsed_ms=_elapsed_ms(started_at), attrs=attrs)

    def add(
        self,
        name: str,
        *,
        elapsed_ms: int,
        attrs: Mapping[str, object | None] | None = None,
    ) -> None:
        if not self.enabled:
            return
        span = AgentSpan(
            name=name,
            elapsed_ms=max(elapsed_ms, 0),
            attrs=_clean_attrs(attrs or {}),
        )
        self._spans.append(span)
        LOGGER.debug("agent_span name=%s elapsed_ms=%s attrs=%s", span.name, span.elapsed_ms, span.attrs)

    def render(self) -> str:
        if not self._spans:
            return ""
        lines: list[str] = []
        for span in self._spans:
            attrs = ", ".join(f"{key}={value}" for key, value in span.attrs.items())
            line = f"{span.name}: {span.elapsed_ms}ms"
            if attrs:
                line += f" ({attrs})"
            lines.append(line)
        return "\n".join(lines)


def _clean_attrs(attrs: Mapping[str, object | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > MAX_TRACE_VALUE_CHARS:
            text = text[: MAX_TRACE_VALUE_CHARS - 12].rstrip() + " [truncated]"
        cleaned[key] = text
    return cleaned


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
