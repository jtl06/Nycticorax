from __future__ import annotations

import re
from dataclasses import dataclass

from nycti.config import Settings
from nycti.formatting import parse_json_object_payload
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.memory.filtering import (
    ALLOWED_MEMORY_CATEGORIES,
    contains_transient_memory_pattern,
    has_explicit_working_memory_directive,
    has_guild_lore_signal,
    has_memory_retraction_signal,
    has_useful_memory_signal,
    should_skip_memory_extraction,
)
from nycti.memory.visibility import MemoryVisibility
from nycti.memory.lifecycle import (
    MemoryKind,
    MemoryOperation,
    normalize_memory_kind,
    normalize_memory_operation,
    normalize_predicate,
    normalize_related_entities,
)

MEMORY_CONFIDENCE_GRACE = 0.12


def coerce_json_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


@dataclass(slots=True)
class MemoryCandidate:
    summary: str
    category: str
    confidence: float
    tags: list[str]
    source_excerpt: str
    suggested_visibility: MemoryVisibility = MemoryVisibility.PRIVATE
    memory_kind: MemoryKind = MemoryKind.FACT
    operation: MemoryOperation = MemoryOperation.UPSERT
    predicate: str = "general_fact"
    object_text: str = ""
    related_entities: tuple[str, ...] = ()
    ttl_days: int | None = None


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
        availability_check = getattr(self.llm_client, "is_model_available", None)
        if callable(availability_check) and not availability_check(self.settings.openai_memory_model):
            return None, None

        result = await self.llm_client.complete_chat(
            model=self.settings.openai_memory_model,
            feature="memory_extract",
            max_tokens=360,
            temperature=0,
            request_timeout_seconds=8.0,
            request_max_retries=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You decide whether a Discord message should become long-term memory. "
                        "Store only durable, non-sensitive details that are likely to matter well beyond the current conversation. "
                        "The current message is authored by the memory owner. Store facts about that author only when they are stated in the current message; use recent context only to resolve references, never as evidence about the author. "
                        "Prefer stable personal preferences, career goals, target jobs or companies, ongoing projects, recurring plans, routines, identity facts, and useful friend-server lore. "
                        "Do not store temporary shopping intent, current deal-hunting, promo or discount requests, one-off recommendation criteria, exact link-format requests, or other short-lived task state. "
                        "Allowed categories: preference, plan, project, lore. Memory kinds are fact, episode, or working. Use fact for stable attributes, episode for a durable event or decision, and working only for an explicitly requested temporary reminder/context. "
                        "Return a stable snake_case predicate naming the attribute, such as preferred_editor, employer, current_project, or meetup_day. Use the same predicate when a message updates or retracts an earlier fact. "
                        "Set operation=retract only when the author explicitly says a prior fact is no longer true and gives no replacement; otherwise use operation=upsert with the current value. "
                        "Visibility must be private or lore. Default to private. Choose lore only for an explicitly shared server-wide convention, tradition, or running joke, never for a person's private fact. "
                        "Never store secrets, credentials, financial data, legal identifiers, or one-off chatter. "
                        "Return JSON only with keys: should_store, confidence, category, memory, tags, visibility, contains_sensitive, memory_kind, operation, predicate, value, related_entities, ttl_days."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Current message:\n{current_message}\n\n"
                        f"Recent context:\n{recent_context or '(none)'}\n\n"
                        f"Local heuristic result: {reason}.\n"
                        "If the message is not worth saving, set should_store to false and memory to an empty string. "
                        "If there is a clear durable fact or goal, prefer a short normalized memory like 'Wants to work at Optiver' or 'Prefers lowercase mat'. "
                        "List only durable named people, projects, organizations, games, or places in related_entities. "
                        "For working memory, ttl_days must be 1-30 and the author must explicitly ask Nycti to remember it temporarily. "
                        "Reject shopping or link-request summaries like 'Wants a free phone deal' or 'Wants official Cartier product page links'. "
                        "Keep memory under 180 characters and tags under 5 short keywords."
                    ),
                },
            ],
        )
        payload = parse_json_object_payload(result.text)
        if not payload:
            return None, result

        should_store = coerce_json_bool(payload.get("should_store"))
        contains_sensitive = coerce_json_bool(payload.get("contains_sensitive"))
        category = str(payload.get("category", "")).strip().lower()
        summary = re.sub(r"\s+", " ", str(payload.get("memory", "")).strip())
        confidence = self._coerce_confidence(payload.get("confidence"))
        tags = [str(tag).strip().lower() for tag in payload.get("tags", []) if str(tag).strip()]
        requested_visibility = str(payload.get("visibility", MemoryVisibility.PRIVATE.value)).strip().lower()
        suggested_visibility = (
            MemoryVisibility.LORE
            if requested_visibility == MemoryVisibility.LORE.value
            and category == "lore"
            and has_guild_lore_signal(current_message)
            else MemoryVisibility.PRIVATE
        )
        memory_kind = normalize_memory_kind(payload.get("memory_kind"), category=category)
        if suggested_visibility is MemoryVisibility.LORE:
            memory_kind = MemoryKind.LORE
        elif memory_kind is MemoryKind.WORKING and not has_explicit_working_memory_directive(
            current_message
        ):
            memory_kind = MemoryKind.FACT
        operation = normalize_memory_operation(payload.get("operation"))
        if operation is MemoryOperation.RETRACT and (
            not has_memory_retraction_signal(current_message) or bool(summary)
        ):
            operation = MemoryOperation.UPSERT
        predicate = normalize_predicate(
            payload.get("predicate"),
            fallback=(tags[0] if tags else summary),
        )
        object_text = re.sub(
            r"\s+",
            " ",
            str(payload.get("value", "") or summary).strip(),
        )[:180]
        related_entities = normalize_related_entities(payload.get("related_entities"))
        ttl_days = self._coerce_ttl_days(payload.get("ttl_days"))
        if memory_kind is not MemoryKind.WORKING:
            ttl_days = None
        has_strong_signal = has_useful_memory_signal(current_message)
        effective_threshold = max(
            0.0,
            self.settings.memory_confidence_threshold - (MEMORY_CONFIDENCE_GRACE if has_strong_signal else 0.0),
        )

        if not should_store or contains_sensitive:
            return None, result
        if category not in ALLOWED_MEMORY_CATEGORIES:
            return None, result
        if confidence < effective_threshold:
            return None, result
        if not summary and operation is not MemoryOperation.RETRACT:
            return None, result
        if contains_transient_memory_pattern(summary):
            return None, result

        excerpt = current_message.strip()
        if len(excerpt) > 280:
            excerpt = f"{excerpt[:277]}..."

        return (
            MemoryCandidate(
                summary=summary[:180],
                category=category,
                confidence=confidence,
                tags=[tag[:32] for tag in tags[:5]],
                source_excerpt=excerpt,
                suggested_visibility=suggested_visibility,
                memory_kind=memory_kind,
                operation=operation,
                predicate=predicate,
                object_text=object_text,
                related_entities=related_entities,
                ttl_days=ttl_days,
            ),
            result,
        )

    def _coerce_confidence(self, value: object) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _coerce_ttl_days(value: object) -> int | None:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if 1 <= parsed <= 30 else None
