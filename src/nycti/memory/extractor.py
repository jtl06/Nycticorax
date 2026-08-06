from __future__ import annotations

import re
from dataclasses import dataclass, replace

from nycti.config import Settings
from nycti.formatting import parse_json_object_payload
from nycti.llm.client import LLMResult, OpenAIClient
from nycti.memory.filtering import (
    ALLOWED_MEMORY_CATEGORIES,
    contains_transient_memory_pattern,
    has_explicit_working_memory_directive,
    has_guild_lore_signal,
    has_guild_shared_configuration_signal,
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
MAX_MEMORY_SUMMARY_CHARS = 320
MAX_MEMORY_VALUE_CHARS = 320
MAX_MEMORY_SOURCE_EXCERPT_CHARS = 600
MAX_MEMORY_TAGS = 8
MAX_MEMORY_TAG_CHARS = 40
MAX_TICKER_INTERESTS_PER_MESSAGE = 12
TICKER_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9]{0,8}(?:[.-][A-Z0-9]{1,4})?")


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
        candidates, result = await self.extract_many(
            current_message=current_message,
            recent_context=recent_context,
        )
        return (candidates[0] if candidates else None), result

    async def extract_many(
        self,
        *,
        current_message: str,
        recent_context: str,
    ) -> tuple[list[MemoryCandidate], LLMResult | None]:
        skip, reason = should_skip_memory_extraction(current_message)
        if skip:
            return [], None
        availability_check = getattr(self.llm_client, "is_model_available", None)
        if callable(availability_check) and not availability_check(self.settings.openai_memory_model):
            return [], None

        result = await self.llm_client.complete_chat(
            model=self.settings.openai_memory_model,
            feature="memory_extract",
            max_tokens=560,
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
                        "A stable interest in following a specifically named stock ticker is an acceptable private preference. Never store holdings, position size, cost basis, transactions, account balances, or other financial data. "
                        "Do not store temporary shopping intent, current deal-hunting, promo or discount requests, one-off recommendation criteria, exact link-format requests, or other short-lived task state. "
                        "Allowed categories: preference, plan, project, lore. Memory kinds are fact, episode, or working. Use fact for stable attributes, episode for a durable event or decision, and working only for an explicitly requested temporary reminder/context. "
                        "Return a stable snake_case predicate naming the attribute, such as preferred_editor, employer, current_project, or meetup_day. Use the same predicate when a message updates or retracts an earlier fact. "
                        "Set operation=retract only when the author explicitly says a prior fact is no longer true and gives no replacement; otherwise use operation=upsert with the current value. "
                        "Visibility may be private, guild_shared, or lore. Default to private. Choose guild_shared only when the author explicitly defines a future server-wide bot default or shared configuration, such as the tickers to include in future server market reports. Choose lore only for an explicitly shared server-wide convention, tradition, or running joke. Never expose a personal fact, goal, profile, holding, or ordinary preference as guild_shared or lore. "
                        "Never store secrets, credentials, financial data, legal identifiers, or one-off chatter. "
                        "For personal stock-ticker interests, use category=preference, visibility=private, memory_kind=fact, predicate=stock_ticker_interest. For an explicit shared market-report default, use visibility=guild_shared and predicate=shared_market_report_ticker. Return every explicitly written ticker in ticker_symbols (maximum 12); never infer one. "
                        "Return JSON only with keys: should_store, confidence, category, memory, tags, visibility, contains_sensitive, memory_kind, operation, predicate, value, related_entities, ticker_symbols, ttl_days."
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
                        "Keep memory under 320 characters and tags under 8 short keywords."
                    ),
                },
            ],
        )
        payload = parse_json_object_payload(result.text)
        if not payload:
            return [], result

        should_store = coerce_json_bool(payload.get("should_store"))
        contains_sensitive = coerce_json_bool(payload.get("contains_sensitive"))
        category = str(payload.get("category", "")).strip().lower()
        summary = re.sub(r"\s+", " ", str(payload.get("memory", "")).strip())
        confidence = self._coerce_confidence(payload.get("confidence"))
        tags = [str(tag).strip().lower() for tag in payload.get("tags", []) if str(tag).strip()]
        requested_visibility = str(payload.get("visibility", MemoryVisibility.PRIVATE.value)).strip().lower()
        if (
            requested_visibility == MemoryVisibility.LORE.value
            and category == "lore"
            and has_guild_lore_signal(current_message)
        ):
            suggested_visibility = MemoryVisibility.LORE
        elif (
            requested_visibility == MemoryVisibility.GUILD_SHARED.value
            and category in {"preference", "project"}
            and has_guild_shared_configuration_signal(current_message)
        ):
            suggested_visibility = MemoryVisibility.GUILD_SHARED
        else:
            suggested_visibility = MemoryVisibility.PRIVATE
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
        )[:MAX_MEMORY_VALUE_CHARS]
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
            return [], result
        if category not in ALLOWED_MEMORY_CATEGORIES:
            return [], result
        if confidence < effective_threshold:
            return [], result
        if not summary and operation is not MemoryOperation.RETRACT:
            return [], result
        if contains_transient_memory_pattern(summary):
            return [], result

        excerpt = current_message.strip()
        if len(excerpt) > MAX_MEMORY_SOURCE_EXCERPT_CHARS:
            excerpt = f"{excerpt[: MAX_MEMORY_SOURCE_EXCERPT_CHARS - 3]}..."

        candidate = MemoryCandidate(
            summary=summary[:MAX_MEMORY_SUMMARY_CHARS],
            category=category,
            confidence=confidence,
            tags=[tag[:MAX_MEMORY_TAG_CHARS] for tag in tags[:MAX_MEMORY_TAGS]],
            source_excerpt=excerpt,
            suggested_visibility=suggested_visibility,
            memory_kind=memory_kind,
            operation=operation,
            predicate=predicate,
            object_text=object_text,
            related_entities=related_entities,
            ttl_days=ttl_days,
        )
        ticker_candidates = self._ticker_interest_candidates(
            payload=payload,
            candidate=candidate,
            current_message=current_message,
            recent_context=recent_context,
        )
        if ticker_candidates is not None:
            return ticker_candidates, result
        return [candidate], result

    @staticmethod
    def _ticker_interest_candidates(
        *,
        payload: dict[str, object],
        candidate: MemoryCandidate,
        current_message: str,
        recent_context: str,
    ) -> list[MemoryCandidate] | None:
        raw_symbols = payload.get("ticker_symbols")
        if not isinstance(raw_symbols, list) or not raw_symbols:
            return None
        if candidate.category != "preference" or candidate.memory_kind is not MemoryKind.FACT:
            return []
        shared = candidate.suggested_visibility is MemoryVisibility.GUILD_SHARED
        if candidate.suggested_visibility not in {
            MemoryVisibility.PRIVATE,
            MemoryVisibility.GUILD_SHARED,
        }:
            return []

        symbols: list[str] = []
        for raw_symbol in raw_symbols:
            symbol = str(raw_symbol).strip().removeprefix("$").upper()
            if (
                not TICKER_SYMBOL_RE.fullmatch(symbol)
                or symbol in symbols
                or not _message_mentions_ticker(current_message, symbol)
                or _ambiguous_with_context_speaker(
                    current_message=current_message,
                    recent_context=recent_context,
                    symbol=symbol,
                )
            ):
                continue
            symbols.append(symbol)
            if len(symbols) >= MAX_TICKER_INTERESTS_PER_MESSAGE:
                break

        return [
            replace(
                candidate,
                summary=(
                    ""
                    if candidate.operation is MemoryOperation.RETRACT
                    else (
                        f"Include {symbol} in shared market reports"
                        if shared
                        else f"Follows {symbol} as a stock ticker of interest"
                    )
                ),
                tags=[
                    "stock",
                    "ticker",
                    "shared_watchlist" if shared else "watchlist",
                    symbol.casefold(),
                ],
                predicate=normalize_predicate(
                    (
                        f"shared_market_report_ticker_{symbol}"
                        if shared
                        else f"stock_ticker_interest_{symbol}"
                    ),
                    fallback=(
                        "shared_market_report_ticker"
                        if shared
                        else "stock_ticker_interest"
                    ),
                ),
                object_text=symbol,
                related_entities=normalize_related_entities([symbol]),
            )
            for symbol in symbols
        ]

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


def _message_mentions_ticker(message: str, symbol: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])",
        message,
        re.IGNORECASE,
    ) is not None


def _ambiguous_with_context_speaker(
    *,
    current_message: str,
    recent_context: str,
    symbol: str,
) -> bool:
    if re.search(rf"(?<![A-Za-z0-9])\${re.escape(symbol)}(?![A-Za-z0-9])", current_message, re.I):
        return False
    if re.search(
        rf"(?i)\b(?:stock|ticker|symbol|watchlist|market\s+reports?)\b[^\n]{{0,80}}\b{re.escape(symbol)}\b|"
        rf"\b{re.escape(symbol)}\b[^\n]{{0,80}}\b(?:stock|ticker|symbol|watchlist|market\s+reports?)\b",
        current_message,
    ):
        return False
    for line in recent_context.splitlines():
        prefix, separator, _rest = line.partition(":")
        if not separator:
            continue
        speaker = re.sub(r"^\s*(?:\[[^\]\n]+\]\s*)*", "", prefix).strip()
        compact_speaker = re.sub(r"[^A-Za-z0-9]", "", speaker).upper()
        if re.fullmatch(rf"{re.escape(symbol)}\d*", compact_speaker):
            return True
    return False
