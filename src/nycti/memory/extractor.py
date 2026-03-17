from __future__ import annotations

import json
import re
from dataclasses import dataclass

from nycti.config import Settings
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.memory.filtering import ALLOWED_MEMORY_CATEGORIES, should_skip_memory_extraction


@dataclass(slots=True)
class MemoryCandidate:
    summary: str
    category: str
    confidence: float
    tags: list[str]
    source_excerpt: str


class MemoryExtractor:
    def __init__(self, settings: Settings, llm_client: OpenAIClient) -> None:
        self.settings = settings
        self.llm_client = llm_client

    async def extract(
        self,
        *,
        current_message: str,
        recent_context: str,
    ) -> tuple[MemoryCandidate | None, LLMResult | None]:
        skip, reason = should_skip_memory_extraction(current_message)
        if skip:
            return None, None

        result = await self.llm_client.complete_chat(
            model=self.settings.openai_memory_model,
            feature="memory_extract",
            max_tokens=220,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You decide whether a Discord message should become long-term memory. "
                        "Only store high-value, non-sensitive details that help future replies. "
                        "Allowed categories: preference, plan, project, lore. "
                        "Never store secrets, credentials, financial data, legal identifiers, or one-off chatter. "
                        "Return JSON only with keys: should_store, confidence, category, memory, tags, contains_sensitive."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Current message:\n{current_message}\n\n"
                        f"Recent context:\n{recent_context or '(none)'}\n\n"
                        f"Local heuristic result: {reason}.\n"
                        "If the message is not worth saving, set should_store to false and memory to an empty string. "
                        "Keep memory under 180 characters and tags under 5 short keywords."
                    ),
                },
            ],
        )
        payload = self._parse_json(result.text)
        if not payload:
            return None, result

        should_store = bool(payload.get("should_store"))
        contains_sensitive = bool(payload.get("contains_sensitive"))
        category = str(payload.get("category", "")).strip().lower()
        summary = re.sub(r"\s+", " ", str(payload.get("memory", "")).strip())
        confidence = self._coerce_confidence(payload.get("confidence"))
        tags = [str(tag).strip().lower() for tag in payload.get("tags", []) if str(tag).strip()]

        if not should_store or contains_sensitive:
            return None, result
        if category not in ALLOWED_MEMORY_CATEGORIES:
            return None, result
        if confidence < self.settings.memory_confidence_threshold:
            return None, result
        if not summary:
            return None, result

        excerpt = current_message.strip()
        if len(excerpt) > 280:
            excerpt = f"{excerpt[:277]}..."

        return (
            MemoryCandidate(
                summary=summary[:180],
                category=category,
                confidence=confidence,
                tags=tags[:5],
                source_excerpt=excerpt,
            ),
            result,
        )

    def _coerce_confidence(self, value: object) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    def _parse_json(self, text: str) -> dict[str, object] | None:
        cleaned = text.strip()
        if not cleaned:
            return None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        return data
