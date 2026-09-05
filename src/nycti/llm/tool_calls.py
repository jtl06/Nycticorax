from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str
