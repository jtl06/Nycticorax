from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

if TYPE_CHECKING:
    from nycti.db.session import Database

BAD_BOT_RE = re.compile(r"^\s*bad\s+bot\b", re.IGNORECASE)
FEEDBACK_MAX_AGE = timedelta(minutes=15)
FEEDBACK_CACHE_LIMIT = 24
FEEDBACK_BUNDLE_CHAR_LIMIT = 240_000
LOGGER = logging.getLogger(__name__)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+")


@dataclass(slots=True)
class ResponseDiagnosticSnapshot:
    captured_at: datetime
    guild_id: int
    channel_id: int
    source_message_id: int
    source_message_url: str
    source_user_id: int
    prompt: str
    context_lines: tuple[str, ...]
    image_context_lines: tuple[str, ...]
    reply_text: str
    metrics: dict[str, int | str]
    bot_message_ids: tuple[int, ...] = ()


class ResponseDiagnosticCache:
    def __init__(
        self,
        *,
        max_entries: int = FEEDBACK_CACHE_LIMIT,
        max_age: timedelta = FEEDBACK_MAX_AGE,
    ) -> None:
        self.max_entries = max(max_entries, 1)
        self.max_age = max_age
        self._snapshots: list[ResponseDiagnosticSnapshot] = []
        self._by_message_id: dict[int, ResponseDiagnosticSnapshot] = {}

    def record(self, snapshot: ResponseDiagnosticSnapshot, *, bot_message_ids: list[int]) -> None:
        snapshot.bot_message_ids = tuple(dict.fromkeys(bot_message_ids))
        self._snapshots.append(snapshot)
        for message_id in snapshot.bot_message_ids:
            self._by_message_id[message_id] = snapshot
        self._prune(now=snapshot.captured_at)

    def find(
        self,
        *,
        channel_id: int,
        reference_message_id: int | None,
        now: datetime,
    ) -> ResponseDiagnosticSnapshot | None:
        self._prune(now=now)
        if reference_message_id is not None:
            snapshot = self._by_message_id.get(reference_message_id)
            if snapshot is None or snapshot.channel_id != channel_id:
                return None
            return snapshot
        for snapshot in reversed(self._snapshots):
            if snapshot.channel_id == channel_id:
                return snapshot
        return None

    def _prune(self, *, now: datetime) -> None:
        cutoff = now - self.max_age
        retained = [
            snapshot
            for snapshot in self._snapshots[-self.max_entries :]
            if snapshot.captured_at >= cutoff
        ]
        self._snapshots = retained
        self._by_message_id = {
            message_id: snapshot
            for snapshot in retained
            for message_id in snapshot.bot_message_ids
        }


def is_bad_bot_feedback(text: str) -> bool:
    return bool(BAD_BOT_RE.search(text))


async def send_bad_bot_feedback(
    bot,
    *,
    database,
    debug_channel_id: int | None,
    snapshot: ResponseDiagnosticSnapshot,
    feedback_message,
    bundle: str | None = None,
) -> bool:
    from nycti.error_debug import send_error_debug_message

    if debug_channel_id is None:
        return False
    if bundle is None:
        bundle = await build_bad_bot_feedback_bundle(
            database,
            snapshot=snapshot,
            feedback_message_id=feedback_message.id,
            feedback_message_url=feedback_message.jump_url,
            feedback_user_id=feedback_message.author.id,
            feedback_text=feedback_message.content,
        )
    content = (
        "```text\n"
        "nycti_response_feedback\n"
        "rating: bad\n"
        f"source_message_id: {snapshot.source_message_id}\n"
        f"feedback_message_id: {feedback_message.id}\n"
        f"source_message_url: {snapshot.source_message_url}\n"
        f"agent_run_id: {snapshot.metrics.get('agent_run_id', '')}\n"
        "attachment: redacted response replay bundle\n"
        "```"
    )
    return await send_error_debug_message(
        bot,
        channel_id=debug_channel_id,
        content=content,
        attachment_text=bundle,
        attachment_filename=f"nycti-bad-bot-{snapshot.source_message_id}.txt",
    )


async def archive_bad_bot_feedback(
    database: Database,
    *,
    snapshot: ResponseDiagnosticSnapshot,
    feedback_message,
    bundle: str,
) -> bool:
    """Keep explicitly submitted feedback after its short-lived source snapshot expires."""
    if not hasattr(database, "session"):
        return False
    try:
        from nycti.db.models import BadBotFeedbackRecord

        async with database.session() as session:
            row = await session.get(BadBotFeedbackRecord, feedback_message.id)
            payload = {
                "source_message_id": snapshot.source_message_id,
                "guild_id": snapshot.guild_id,
                "channel_id": snapshot.channel_id,
                "source_user_id": snapshot.source_user_id,
                "feedback_user_id": feedback_message.author.id,
                "source_message_url": redact_diagnostic_secrets(snapshot.source_message_url),
                "feedback_message_url": redact_diagnostic_secrets(feedback_message.jump_url),
                "feedback_text": redact_diagnostic_secrets(feedback_message.content),
                "bundle": redact_diagnostic_secrets(bundle),
            }
            if row is None:
                session.add(
                    BadBotFeedbackRecord(
                        feedback_message_id=feedback_message.id,
                        **payload,
                    )
                )
            else:
                for key, value in payload.items():
                    setattr(row, key, value)
            await session.commit()
    except Exception:
        LOGGER.warning("Failed to archive bad-bot feedback.", exc_info=True)
        return False
    return True


async def handle_bad_bot_feedback(
    bot,
    *,
    database: Database,
    debug_channel_id: int | None,
    persist_snapshots: bool,
    cache: ResponseDiagnosticCache,
    feedback_message,
) -> bool:
    """Archive and forward a direct-reply feedback report without an LLM call."""
    reference_message_id = getattr(
        getattr(feedback_message, "reference", None),
        "message_id",
        None,
    )
    if reference_message_id is None:
        return False
    snapshot = cache.find(
        channel_id=feedback_message.channel.id,
        reference_message_id=reference_message_id,
        now=datetime.now(timezone.utc),
    )
    if snapshot is None:
        snapshot = await load_persisted_response_diagnostic_snapshot(
            database,
            guild_id=feedback_message.guild.id,
            channel_id=feedback_message.channel.id,
            reference_message_id=reference_message_id,
            now=datetime.now(timezone.utc),
            enabled=persist_snapshots,
        )
    if snapshot is None:
        await feedback_message.reply(
            "I couldn't find a Nycti reply from the last 15 minutes to log. Reply directly to it and try again.",
            mention_author=False,
        )
        return True

    bundle = await build_bad_bot_feedback_bundle(
        database,
        snapshot=snapshot,
        feedback_message_id=feedback_message.id,
        feedback_message_url=feedback_message.jump_url,
        feedback_user_id=feedback_message.author.id,
        feedback_text=feedback_message.content,
    )
    archived = await archive_bad_bot_feedback(
        database,
        snapshot=snapshot,
        feedback_message=feedback_message,
        bundle=bundle,
    )
    sent = await send_bad_bot_feedback(
        bot,
        database=database,
        debug_channel_id=debug_channel_id,
        snapshot=snapshot,
        feedback_message=feedback_message,
        bundle=bundle,
    )
    LOGGER.info(
        "Bad-bot feedback source_message_id=%s feedback_message_id=%s archived=%s debug_sent=%s.",
        snapshot.source_message_id,
        feedback_message.id,
        archived,
        sent,
    )
    if sent:
        await feedback_message.reply("Logged that response for review.", mention_author=False)
    elif archived:
        await feedback_message.reply(
            "Saved those diagnostics for review, but couldn't send them to the debug channel.",
            mention_author=False,
        )
    else:
        await feedback_message.reply(
            "I found that response, but couldn't send its diagnostics to the debug channel.",
            mention_author=False,
        )
    return True


async def persist_response_diagnostic_snapshot(
    database: Database,
    *,
    snapshot: ResponseDiagnosticSnapshot,
    enabled: bool = False,
) -> bool:
    """Persist an explicitly enabled, redacted snapshot with a fixed expiry."""
    if not enabled or not hasattr(database, "session"):
        return False
    try:
        from nycti.db.models import (
            ResponseDiagnosticMessageRecord,
            ResponseDiagnosticSnapshotRecord,
        )

        session: Any
        async with database.session() as session:
            await prune_expired_response_diagnostics(
                session,
                now=snapshot.captured_at,
            )
            row = await session.get(
                ResponseDiagnosticSnapshotRecord,
                snapshot.source_message_id,
            )
            payload = _redacted_snapshot_payload(snapshot)
            if row is None:
                row = ResponseDiagnosticSnapshotRecord(
                    source_message_id=snapshot.source_message_id,
                    **payload,
                )
                session.add(row)
            else:
                for key, value in payload.items():
                    setattr(row, key, value)
                await session.execute(
                    delete(ResponseDiagnosticMessageRecord).where(
                        ResponseDiagnosticMessageRecord.source_message_id
                        == snapshot.source_message_id
                    )
                )
            await session.flush()
            session.add_all(
                ResponseDiagnosticMessageRecord(
                    bot_message_id=message_id,
                    source_message_id=snapshot.source_message_id,
                )
                for message_id in dict.fromkeys(snapshot.bot_message_ids)
            )
            await session.commit()
    except Exception:
        LOGGER.warning("Failed to persist short-lived bad-bot diagnostics.", exc_info=True)
        return False
    return True


async def load_persisted_response_diagnostic_snapshot(
    database: Database,
    *,
    guild_id: int,
    channel_id: int,
    reference_message_id: int | None,
    now: datetime,
    enabled: bool = False,
) -> ResponseDiagnosticSnapshot | None:
    if not enabled or not hasattr(database, "session"):
        return None
    try:
        from nycti.db.models import (
            ResponseDiagnosticMessageRecord,
            ResponseDiagnosticSnapshotRecord,
        )

        session: Any
        async with database.session() as session:
            deleted_count = await prune_expired_response_diagnostics(
                session,
                now=now,
            )
            statement = select(ResponseDiagnosticSnapshotRecord).where(
                ResponseDiagnosticSnapshotRecord.guild_id == guild_id,
                ResponseDiagnosticSnapshotRecord.channel_id == channel_id,
                ResponseDiagnosticSnapshotRecord.expires_at > now,
            )
            if reference_message_id is not None:
                statement = statement.join(
                    ResponseDiagnosticMessageRecord,
                    ResponseDiagnosticMessageRecord.source_message_id
                    == ResponseDiagnosticSnapshotRecord.source_message_id,
                ).where(
                    ResponseDiagnosticMessageRecord.bot_message_id
                    == reference_message_id,
                )
            else:
                statement = statement.order_by(
                    ResponseDiagnosticSnapshotRecord.captured_at.desc()
                )
            row = await session.scalar(statement.limit(1))
            bot_message_ids: tuple[int, ...] = ()
            if row is not None:
                bot_message_ids = tuple(
                    await session.scalars(
                        select(ResponseDiagnosticMessageRecord.bot_message_id)
                        .where(
                            ResponseDiagnosticMessageRecord.source_message_id
                            == row.source_message_id
                        )
                        .order_by(ResponseDiagnosticMessageRecord.bot_message_id)
                    )
                )
            if deleted_count > 0:
                await session.commit()
    except Exception:
        LOGGER.warning("Failed to load persisted bad-bot diagnostics.", exc_info=True)
        return None
    if row is None:
        return None
    return _snapshot_from_record(row, bot_message_ids=bot_message_ids)


async def prune_expired_response_diagnostics(
    session,
    *,
    now: datetime,
) -> int:
    from nycti.db.models import (
        ResponseDiagnosticMessageRecord,
        ResponseDiagnosticSnapshotRecord,
    )

    expired_source_ids = select(
        ResponseDiagnosticSnapshotRecord.source_message_id
    ).where(ResponseDiagnosticSnapshotRecord.expires_at <= now)
    await session.execute(
        delete(ResponseDiagnosticMessageRecord).where(
            ResponseDiagnosticMessageRecord.source_message_id.in_(
                expired_source_ids
            )
        )
    )
    result = await session.execute(
        delete(ResponseDiagnosticSnapshotRecord).where(
            ResponseDiagnosticSnapshotRecord.expires_at <= now,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def build_bad_bot_feedback_bundle(
    database,
    *,
    snapshot: ResponseDiagnosticSnapshot,
    feedback_message_id: int,
    feedback_message_url: str,
    feedback_user_id: int,
    feedback_text: str,
) -> str:
    run_id = str(snapshot.metrics.get("agent_run_id", "") or "")
    run_lines = await _load_run_telemetry(database, run_id=run_id)
    public_metrics = {
        key: value
        for key, value in snapshot.metrics.items()
        if not key.startswith("_") and not key.endswith("_request_json")
    }
    sections = [
        "nycti_bad_bot_feedback_bundle",
        f"captured_at: {datetime.now(timezone.utc).isoformat()}",
        f"guild_id: {snapshot.guild_id}",
        f"channel_id: {snapshot.channel_id}",
        f"source_message_id: {snapshot.source_message_id}",
        f"source_message_url: {snapshot.source_message_url}",
        f"source_user_id: {snapshot.source_user_id}",
        f"bot_message_ids: {', '.join(str(value) for value in snapshot.bot_message_ids)}",
        f"feedback_message_id: {feedback_message_id}",
        f"feedback_message_url: {feedback_message_url}",
        f"feedback_user_id: {feedback_user_id}",
        f"feedback: {feedback_text}",
        "",
        "original_request",
        snapshot.prompt,
        "",
        "bounded_discord_context",
        *(snapshot.context_lines or ("(none)",)),
        "",
        "image_context",
        *(snapshot.image_context_lines or ("(none)",)),
        "",
        "nycti_reply",
        snapshot.reply_text,
        "",
        "run_metrics",
        json.dumps(public_metrics, ensure_ascii=True, indent=2, sort_keys=True),
        "",
        "agent_messages_and_tool_results",
        str(snapshot.metrics.get("_diagnostic_agent_messages_json", "(unavailable)")),
        "",
        "exposed_tool_schemas",
        str(snapshot.metrics.get("_diagnostic_tool_schemas_json", "(unavailable)")),
        "",
        "persisted_run_telemetry",
        *run_lines,
        "",
        "notes",
        "- The bounded snapshot was captured when Nycti sent the response; this bundle was emitted only after explicit `bad bot` feedback.",
        "- Context is bounded to the original request payload; obvious credentials and embedded images are redacted.",
    ]
    bundle = redact_diagnostic_secrets("\n".join(sections))
    if len(bundle) > FEEDBACK_BUNDLE_CHAR_LIMIT:
        bundle = bundle[: FEEDBACK_BUNDLE_CHAR_LIMIT - 16].rstrip() + "\n[truncated]"
    return bundle


async def _load_run_telemetry(database, *, run_id: str) -> list[str]:
    if not run_id or not hasattr(database, "session"):
        return ["(unavailable)"]
    try:
        from sqlalchemy import select

        from nycti.db.models import AgentRunEvent, AgentStepEvent

        async with database.session() as session:
            run_event = (
                await session.execute(
                    select(AgentRunEvent).where(AgentRunEvent.run_id == run_id)
                )
            ).scalar_one_or_none()
            steps = (
                await session.execute(
                    select(AgentStepEvent)
                    .where(AgentStepEvent.run_id == run_id)
                    .order_by(AgentStepEvent.step_index)
                )
            ).scalars().all()
    except Exception:
        return ["(failed to load correlated telemetry)"]
    lines = []
    if run_event is not None:
        lines.append(
            "run "
            f"status={run_event.final_status} stop={run_event.stop_reason or ''} "
            f"failure={run_event.failure_reason or ''} turns={run_event.model_turn_count} "
            f"tools={run_event.tool_call_count} ms={run_event.latency_ms}"
        )
    lines.append("idx | state | feature | provider | model | tool | status | stop | ms | tokens | details")
    for row in steps:
        lines.append(
            " | ".join(
                (
                    str(row.step_index),
                    row.state,
                    row.feature or "",
                    row.provider or "",
                    row.active_model or row.requested_model or "",
                    row.tool_name or "",
                    row.status or "",
                    row.stop_reason or "",
                    str(row.latency_ms),
                    str(row.total_tokens),
                    json.dumps(row.details, ensure_ascii=True, sort_keys=True),
                )
            )
        )
    return lines or ["(none)"]


def redact_diagnostic_secrets(text: str) -> str:
    redacted = SECRET_ASSIGNMENT_RE.sub(r'\1"[redacted]"', text)
    return BEARER_RE.sub("Bearer [redacted]", redacted)


def _redacted_snapshot_payload(
    snapshot: ResponseDiagnosticSnapshot,
) -> dict[str, object]:
    captured_at = _as_utc(snapshot.captured_at)
    metrics = _redact_snapshot_value(snapshot.metrics)
    return {
        "guild_id": snapshot.guild_id,
        "channel_id": snapshot.channel_id,
        "source_user_id": snapshot.source_user_id,
        "source_message_url": redact_diagnostic_secrets(snapshot.source_message_url),
        "captured_at": captured_at,
        "expires_at": captured_at + FEEDBACK_MAX_AGE,
        "prompt": redact_diagnostic_secrets(snapshot.prompt),
        "context_lines": [
            redact_diagnostic_secrets(line) for line in snapshot.context_lines
        ],
        "image_context_lines": [
            redact_diagnostic_secrets(line) for line in snapshot.image_context_lines
        ],
        "reply_text": redact_diagnostic_secrets(snapshot.reply_text),
        "metrics": metrics if isinstance(metrics, dict) else {},
    }


def _snapshot_from_record(
    row,
    *,
    bot_message_ids: tuple[int, ...],
) -> ResponseDiagnosticSnapshot:
    raw_metrics = row.metrics if isinstance(row.metrics, dict) else {}
    metrics = {
        str(key): item
        for key, item in raw_metrics.items()
        if type(item) in {int, str}
    }
    return ResponseDiagnosticSnapshot(
        captured_at=_as_utc(row.captured_at),
        guild_id=int(row.guild_id),
        channel_id=int(row.channel_id),
        source_message_id=int(row.source_message_id),
        source_message_url=str(row.source_message_url),
        source_user_id=int(row.source_user_id),
        prompt=str(row.prompt),
        context_lines=tuple(str(item) for item in (row.context_lines or ())),
        image_context_lines=tuple(
            str(item) for item in (row.image_context_lines or ())
        ),
        reply_text=str(row.reply_text),
        metrics=metrics,
        bot_message_ids=bot_message_ids,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _redact_snapshot_value(value: object) -> object:
    if isinstance(value, str):
        return redact_diagnostic_secrets(value)
    if isinstance(value, list):
        return [_redact_snapshot_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact_snapshot_value(item)
            for key, item in value.items()
        }
    return value
