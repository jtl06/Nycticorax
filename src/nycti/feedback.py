from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

BAD_BOT_RE = re.compile(r"^\s*bad\s+bot\b", re.IGNORECASE)
FEEDBACK_MAX_AGE = timedelta(minutes=15)
FEEDBACK_CACHE_LIMIT = 24
FEEDBACK_BUNDLE_CHAR_LIMIT = 240_000
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
) -> bool:
    from nycti.error_debug import send_error_debug_message

    if debug_channel_id is None:
        return False
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
    await send_error_debug_message(
        bot,
        channel_id=debug_channel_id,
        content=content,
        attachment_text=bundle,
        attachment_filename=f"nycti-bad-bot-{snapshot.source_message_id}.txt",
    )
    return True


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
        "- Captured only after explicit `bad bot` feedback on a recent Nycti response.",
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
