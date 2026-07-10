from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import LiveBenchmarkAttemptRecord
from nycti.feedback import redact_diagnostic_secrets

LIVE_BENCHMARK_RETENTION = timedelta(days=90)
FAILURE_ARTIFACT_BYTE_LIMIT = 240_000
_FAILURE_STATUSES = frozenset({"fail", "error"})
_VALID_STATUSES = frozenset({"pass", "fail", "error", "skip"})
_MAX_CONTAINER_ITEMS = 256
_MAX_STRING_CHARS = 100_000
_DROP = object()

_DROPPED_KEYS = frozenset(
    {
        "channel_id",
        "continuation",
        "continuation_state",
        "discord_id",
        "encrypted_content",
        "guild_id",
        "hidden_reasoning",
        "image",
        "image_url",
        "image_urls",
        "images",
        "input_image",
        "message_id",
        "previous_response_id",
        "raw_request",
        "reasoning_content",
        "reasoning_summary",
        "request_payload",
        "response_id",
        "responses_output_items",
        "source_message_id",
        "source_message_url",
        "source_user_id",
        "user_id",
    }
)
_DROPPED_ITEM_TYPES = frozenset(
    {
        "computer_screenshot",
        "image",
        "image_url",
        "input_image",
        "reasoning",
    }
)
_ESSENTIAL_ARTIFACT_KEYS = (
    "schema_version",
    "batch_id",
    "suite_version",
    "case_id",
    "attempt_index",
    "mode",
    "status",
    "score",
    "max_score",
    "failed_checks",
    "prompt",
    "answer",
    "error",
    "agent_run_id",
    "tools_called",
    "metrics",
    "agent_trace",
    "diagnostic_agent_messages_json",
    "diagnostic_agent_steps_json",
    "tool_schemas_json",
)


@dataclass(frozen=True, slots=True)
class LiveBenchmarkAttemptInput:
    batch_id: str
    suite_version: str
    case_id: str
    attempt_index: int
    mode: str
    status: str
    score: float
    max_score: float
    failed_checks: tuple[str, ...] = ()
    agent_run_id: str | None = None
    model: str | None = None
    provider: str | None = None
    profile: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    tools_called: tuple[str, ...] = ()
    error: str | None = None
    failure_artifact: Mapping[str, object] | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LiveBenchmarkFailureSummary:
    id: int
    batch_id: str
    suite_version: str
    case_id: str
    attempt_index: int
    mode: str
    status: str
    score: float
    max_score: float
    failed_checks: tuple[str, ...]
    agent_run_id: str | None
    model: str | None
    provider: str | None
    profile: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    tools_called: tuple[str, ...]
    error: str | None
    created_at: datetime
    expires_at: datetime


async def save_live_benchmark_attempt(
    database: Any,
    *,
    attempt: LiveBenchmarkAttemptInput,
) -> int:
    """Commit one attempt immediately and return its durable row id."""

    row = build_live_benchmark_attempt_record(attempt)
    async with database.session() as session:
        session.add(row)
        await session.flush()
        row_id = int(row.id)
        await session.commit()
    return row_id


def build_live_benchmark_attempt_record(
    attempt: LiveBenchmarkAttemptInput,
) -> LiveBenchmarkAttemptRecord:
    status = attempt.status.strip().lower()
    if status not in _VALID_STATUSES:
        raise ValueError(f"Unsupported live benchmark status: {attempt.status!r}")
    if attempt.attempt_index < 1:
        raise ValueError("Live benchmark attempt_index must be at least 1")

    batch_id = _required_text(attempt.batch_id, field="batch_id", limit=64)
    suite_version = _required_text(
        attempt.suite_version,
        field="suite_version",
        limit=32,
    )
    case_id = _required_text(attempt.case_id, field="case_id", limit=96)
    mode = _required_text(attempt.mode, field="mode", limit=16).lower()
    created_at = _as_utc(attempt.created_at or datetime.now(timezone.utc))
    failed_checks = _clean_text_sequence(attempt.failed_checks, limit=500)
    tools_called = _clean_text_sequence(attempt.tools_called, limit=64)
    error = _optional_redacted_text(attempt.error, limit=20_000)
    score = _finite_number(attempt.score, field="score")
    max_score = _finite_number(attempt.max_score, field="max_score")

    artifact_json: str | None = None
    if status in _FAILURE_STATUSES:
        artifact_payload = dict(attempt.failure_artifact or {})
        artifact_payload.update(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "suite_version": suite_version,
                "case_id": case_id,
                "attempt_index": attempt.attempt_index,
                "mode": mode,
                "status": status,
                "score": score,
                "max_score": max_score,
                "failed_checks": failed_checks,
                "agent_run_id": _optional_text(attempt.agent_run_id, limit=64),
                "tools_called": tools_called,
                "error": error,
            }
        )
        artifact_json = serialize_live_benchmark_failure_artifact(artifact_payload)

    return LiveBenchmarkAttemptRecord(
        batch_id=batch_id,
        suite_version=suite_version,
        case_id=case_id,
        attempt_index=attempt.attempt_index,
        mode=mode,
        status=status,
        score=score,
        max_score=max_score,
        failed_checks=failed_checks,
        agent_run_id=_optional_text(attempt.agent_run_id, limit=64),
        model=_optional_text(attempt.model, limit=255),
        provider=_optional_text(attempt.provider, limit=64),
        profile=_optional_text(attempt.profile, limit=32),
        prompt_tokens=max(int(attempt.prompt_tokens), 0),
        completion_tokens=max(int(attempt.completion_tokens), 0),
        total_tokens=max(int(attempt.total_tokens), 0),
        latency_ms=max(int(attempt.latency_ms), 0),
        tools_called=tools_called,
        error=error,
        failure_artifact_json=artifact_json,
        created_at=created_at,
        expires_at=created_at + LIVE_BENCHMARK_RETENTION,
    )


def serialize_live_benchmark_failure_artifact(
    payload: Mapping[str, object],
) -> str:
    """Redact and bound a self-contained failure trace for durable storage."""

    safe = _sanitize_value(payload, key=None, depth=0)
    if not isinstance(safe, dict):  # pragma: no cover - Mapping guarantees this
        safe = {"artifact": safe}
    rendered = _render_json(safe)
    original_size = len(rendered.encode("utf-8"))
    if original_size <= FAILURE_ARTIFACT_BYTE_LIMIT:
        return rendered

    compact = _sanitize_value(
        safe,
        key=None,
        depth=0,
        max_items=64,
        max_string_chars=8_000,
        max_depth=6,
    )
    if isinstance(compact, dict):
        compact["artifact_truncated"] = True
        compact["original_size_bytes"] = original_size
        rendered = _render_json(compact)
        if len(rendered.encode("utf-8")) <= FAILURE_ARTIFACT_BYTE_LIMIT:
            return rendered

    essential_source = safe if isinstance(safe, dict) else {}
    essential = {
        key: essential_source[key]
        for key in _ESSENTIAL_ARTIFACT_KEYS
        if key in essential_source
    }
    bounded_essential = _sanitize_value(
        essential,
        key=None,
        depth=0,
        max_items=24,
        max_string_chars=4_000,
        max_depth=4,
    )
    fallback = {
        "artifact_truncated": True,
        "original_size_bytes": original_size,
        "artifact": bounded_essential,
    }
    rendered = _render_json(fallback)
    if len(rendered.encode("utf-8")) <= FAILURE_ARTIFACT_BYTE_LIMIT:
        return rendered

    # Bound the serialized result itself: JSON escaping can expand Unicode and
    # control-heavy previews substantially.
    preview = rendered[:80_000]
    while True:
        final = _render_json(
            {
                "artifact_truncated": True,
                "original_size_bytes": original_size,
                "preview": preview,
            }
        )
        if len(final.encode("utf-8")) <= FAILURE_ARTIFACT_BYTE_LIMIT:
            return final
        preview = preview[: len(preview) // 2]


async def list_recent_live_benchmark_failures(
    database: Any,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> list[LiveBenchmarkFailureSummary]:
    bounded_limit = min(max(int(limit), 1), 100)
    current_time = _as_utc(now or datetime.now(timezone.utc))
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(LiveBenchmarkAttemptRecord)
                .where(
                    LiveBenchmarkAttemptRecord.status.in_(_FAILURE_STATUSES),
                    LiveBenchmarkAttemptRecord.expires_at > current_time,
                )
                .order_by(
                    LiveBenchmarkAttemptRecord.created_at.desc(),
                    LiveBenchmarkAttemptRecord.id.desc(),
                )
                .limit(bounded_limit)
            )
        )
    return [_failure_summary(row) for row in rows]


async def get_live_benchmark_failure_artifact(
    database: Any,
    *,
    attempt_id: int,
    now: datetime | None = None,
) -> str | None:
    current_time = _as_utc(now or datetime.now(timezone.utc))
    async with database.session() as session:
        return await session.scalar(
            select(LiveBenchmarkAttemptRecord.failure_artifact_json).where(
                LiveBenchmarkAttemptRecord.id == int(attempt_id),
                LiveBenchmarkAttemptRecord.status.in_(_FAILURE_STATUSES),
                LiveBenchmarkAttemptRecord.expires_at > current_time,
            )
        )


async def prune_expired_live_benchmark_attempts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    current_time = _as_utc(now or datetime.now(timezone.utc))
    result = await session.execute(
        delete(LiveBenchmarkAttemptRecord).where(
            LiveBenchmarkAttemptRecord.expires_at <= current_time
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _failure_summary(row: LiveBenchmarkAttemptRecord) -> LiveBenchmarkFailureSummary:
    return LiveBenchmarkFailureSummary(
        id=int(row.id),
        batch_id=row.batch_id,
        suite_version=row.suite_version,
        case_id=row.case_id,
        attempt_index=row.attempt_index,
        mode=row.mode,
        status=row.status,
        score=row.score,
        max_score=row.max_score,
        failed_checks=tuple(str(item) for item in (row.failed_checks or ())),
        agent_run_id=row.agent_run_id,
        model=row.model,
        provider=row.provider,
        profile=row.profile,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        latency_ms=row.latency_ms,
        tools_called=tuple(str(item) for item in (row.tools_called or ())),
        error=row.error,
        created_at=_as_utc(row.created_at),
        expires_at=_as_utc(row.expires_at),
    )


def _sanitize_value(
    value: object,
    *,
    key: str | None,
    depth: int,
    max_items: int = _MAX_CONTAINER_ITEMS,
    max_string_chars: int = _MAX_STRING_CHARS,
    max_depth: int = 10,
) -> object:
    if depth > max_depth:
        return "[maximum depth omitted]"
    if isinstance(value, Mapping):
        item_type = str(value.get("type", "")).strip().lower()
        if item_type in _DROPPED_ITEM_TYPES:
            return "[image/reasoning item omitted]"
        result: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= max_items:
                result["_additional_items_omitted"] = len(value) - max_items
                break
            safe_key = str(raw_key)
            normalized_key = safe_key.strip().lower()
            if _should_drop_key(normalized_key):
                continue
            safe_item = _sanitize_value(
                item,
                key=normalized_key,
                depth=depth + 1,
                max_items=max_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
            )
            if safe_item is not _DROP:
                result[safe_key] = safe_item
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rendered = [
            _sanitize_value(
                item,
                key=key,
                depth=depth + 1,
                max_items=max_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            rendered.append(f"[{len(value) - max_items} additional items omitted]")
        return [item for item in rendered if item is not _DROP]
    if isinstance(value, str):
        if value.lstrip().lower().startswith("data:image/"):
            return "[image data omitted]"
        if key and key.endswith("_json"):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                safe_decoded = _sanitize_value(
                    decoded,
                    key=None,
                    depth=depth + 1,
                    max_items=max_items,
                    max_string_chars=max_string_chars,
                    max_depth=max_depth,
                )
                return _bounded_json_string(
                    safe_decoded,
                    limit=max_string_chars,
                )
        redacted = redact_diagnostic_secrets(value)
        return _truncate(redacted, max_string_chars)
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray)):
        return "[binary data omitted]"
    return _truncate(redact_diagnostic_secrets(str(value)), max_string_chars)


def _should_drop_key(key: str) -> bool:
    if key in _DROPPED_KEYS or key.endswith("_request_json"):
        return True
    return key.endswith(("_channel_id", "_guild_id", "_message_id", "_user_id"))


def _render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _bounded_json_string(value: object, *, limit: int) -> str:
    rendered = _render_json(value)
    if len(rendered) <= limit:
        return rendered
    for max_items, max_string_chars, max_depth in (
        (64, 4_000, 8),
        (32, 1_000, 6),
        (16, 300, 5),
        (8, 100, 4),
    ):
        compact = _sanitize_value(
            value,
            key=None,
            depth=0,
            max_items=max_items,
            max_string_chars=min(max_string_chars, max(limit // 4, 32)),
            max_depth=max_depth,
        )
        rendered = _render_json(compact)
        if len(rendered) <= limit:
            return rendered
    return _render_json({"diagnostic_truncated": True})


def _required_text(value: object, *, field: str, limit: int) -> str:
    rendered = str(value).strip()
    if not rendered:
        raise ValueError(f"Live benchmark {field} must not be empty")
    if len(rendered) > limit:
        raise ValueError(f"Live benchmark {field} exceeds {limit} characters")
    return rendered


def _optional_text(value: object | None, *, limit: int) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered[:limit] or None


def _optional_redacted_text(value: object | None, *, limit: int) -> str | None:
    rendered = _optional_text(value, limit=limit * 2)
    if rendered is None:
        return None
    return _truncate(redact_diagnostic_secrets(rendered), limit)


def _clean_text_sequence(values: Sequence[object], *, limit: int) -> list[str]:
    return list(
        dict.fromkeys(
            cleaned
            for item in values
            if (cleaned := _optional_redacted_text(item, limit=limit)) is not None
        )
    )


def _finite_number(value: float | int | str, *, field: str) -> float:
    rendered = float(value)
    if not math.isfinite(rendered):
        raise ValueError(f"Live benchmark {field} must be finite")
    return rendered


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    suffix = f"...[{omitted} chars omitted]"
    return value[: max(limit - len(suffix), 0)] + suffix


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
