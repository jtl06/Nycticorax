"""Public report receipts attached to delivered-message diagnostics, not user memory."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any

from sqlalchemy import select

RECEIPT_KEY = "_market_report_receipt_json"
RECEIPTS_KEY = "_market_report_receipts_json"
MAX_RECEIPTS = 3
LOGGER = logging.getLogger(__name__)


def report_receipt(report) -> str:
    return json.dumps({
        "requested_symbols": [row.symbol for row in report.rows],
        "requested_session": report.requested_session,
        "rows": [{key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                  for key, value in asdict(row).items() if key != "source"}
                 for row in report.rows],
    }, allow_nan=False)


def merge_report_receipt(metrics: dict, value: str) -> None:
    receipts = json.loads(str(metrics.get(RECEIPTS_KEY, "[]")))
    receipts.append(json.loads(value))
    metrics[RECEIPTS_KEY] = json.dumps(receipts[-MAX_RECEIPTS:])


def _public_receipts(metrics: dict) -> list[dict]:
    try:
        raw = json.loads(str(metrics.get(RECEIPTS_KEY, "[]")))
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    receipts = []
    for item in raw[-MAX_RECEIPTS:]:
        if not isinstance(item, dict) or not isinstance(item.get("rows"), list):
            continue
        rows = [{key: row[key] for key in (
            "symbol", "price", "currency", "percent", "session", "basis", "timestamp", "reference_price", "unavailable"
        ) if key in row} for row in item["rows"][:40] if isinstance(row, dict)]
        receipts.append({"requested_symbols": [row.get("symbol") for row in rows],
                         "requested_session": item.get("requested_session"), "rows": rows})
    return receipts


async def visible_report_context(bot, messages, *, now: datetime | None = None) -> list[str]:
    """Caller supplies already permission-checked messages, with reply targets first."""
    now = now or datetime.now(timezone.utc)
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    cache = getattr(bot, "_response_diagnostic_cache", None)
    candidates: dict[int, Any] = {}
    for message in messages:
        if bot_id is not None and getattr(message.author, "id", None) == bot_id and message.guild:
            candidates.setdefault(message.id, message)
        if len(candidates) >= 12:
            break
    payloads = {}
    for mid, message in candidates.items():
        snapshot = cache.find(channel_id=message.channel.id, reference_message_id=mid, now=now) if cache else None
        if snapshot is not None and snapshot.guild_id == message.guild.id:
            payloads[mid] = _public_receipts(snapshot.metrics)
    missing = [mid for mid in candidates if mid not in payloads]
    database = getattr(bot, "database", None)
    enabled = getattr(getattr(bot, "settings", None), "persist_bad_bot_diagnostics", False)
    if missing and enabled and database is not None:
        from nycti.db.models import ResponseDiagnosticMessageRecord as Link, ResponseDiagnosticSnapshotRecord as Snapshot
        try:
            async with database.session() as session:
                rows = (await session.execute(select(Link.bot_message_id, Snapshot.guild_id, Snapshot.channel_id, Snapshot.metrics)
                    .join(Snapshot, Snapshot.source_message_id == Link.source_message_id)
                    .where(Link.bot_message_id.in_(missing), Snapshot.expires_at > now))).all()
            for mid, guild_id, channel_id, metrics in rows:
                message = candidates[mid]
                if guild_id == message.guild.id and channel_id == message.channel.id:
                    payloads[mid] = _public_receipts(metrics)
        except Exception:
            LOGGER.warning("Could not load public market receipts.", exc_info=True)
    lines = []
    seen = set()
    for mid in candidates:
        if payloads.get(mid):
            payload = json.dumps(payloads[mid])
            if payload in seen:
                continue
            seen.add(payload)
            lines.append(f"[public market report receipt message_id={mid}] " + payload)
        if len(lines) >= MAX_RECEIPTS:
            break
    if lines:
        lines.insert(0, "Report receipts describe already delivered, visible reports, not current quotes. "
                     "Resolve callbacks from the referenced receipt; preserve symbols unless the user changes scope. "
                     "For a fresh update fetch those explicit symbols with the requested session; for a past-answer "
                     "callback reuse the recorded values. Unrelated requests do not inherit report scope. "
                     "If multiple reports are plausible, ask which rather than guessing.")
    return lines
