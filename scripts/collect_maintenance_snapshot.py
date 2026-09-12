"""Bounded, read-only production evidence for Nycti maintenance tasks."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def collect_snapshot(connection, *, guild_id: int, hours: int, limit: int,
                     include_bundles: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT feedback_message_id, created_at, feedback_text, bundle FROM bad_bot_feedback "
            "WHERE created_at >= %s AND guild_id = %s ORDER BY created_at DESC LIMIT %s",
            (since, guild_id, limit),
        )
        incidents = cursor.fetchall()
        if not include_bundles:
            for incident in incidents:
                incident.pop("bundle", None)
        cursor.execute(
            "SELECT final_status, stop_reason, COUNT(*) AS runs, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms, "
            "percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90_ms "
            "FROM agent_run_events WHERE guild_id = %s AND created_at >= %s "
            "GROUP BY final_status, stop_reason ORDER BY runs DESC", (guild_id, since),
        )
        run_summary = cursor.fetchall()
        cursor.execute(
            "SELECT run_id, created_at, final_status, stop_reason, latency_ms, model_turn_count, "
            "tool_call_count, correction_count FROM agent_run_events "
            "WHERE guild_id = %s AND created_at >= %s ORDER BY latency_ms DESC LIMIT %s",
            (guild_id, since, limit),
        )
        slow_runs = cursor.fetchall()
        run_ids = [row["run_id"] for row in slow_runs]
        cursor.execute(
            "SELECT run_id, step_index, state, feature, active_model, provider, tool_name, status, latency_ms, "
            "total_tokens FROM agent_step_events WHERE guild_id = %s AND run_id = ANY(%s) "
            "ORDER BY run_id, step_index LIMIT 500", (guild_id, run_ids),
        )
        steps = cursor.fetchall()
        cursor.execute(
            "SELECT visibility, status, category, COUNT(*) AS memories, "
            "COUNT(*) FILTER (WHERE times_retrieved = 0) AS never_retrieved, "
            "COUNT(*) FILTER (WHERE status = 'active' AND expires_at < CURRENT_TIMESTAMP) AS expired_active, "
            "COUNT(*) FILTER (WHERE embedding IS NULL) AS missing_embedding "
            "FROM memories WHERE guild_id = %s GROUP BY visibility, status, category "
            "ORDER BY visibility, status, category", (guild_id,),
        )
        memory_summary = cursor.fetchall()
        cursor.execute(
            "SELECT id, visibility, status, category, confidence, created_at, last_retrieved_at, times_retrieved "
            "FROM memories WHERE guild_id = %s AND status = 'active' "
            "AND (expires_at < CURRENT_TIMESTAMP OR (times_retrieved = 0 AND created_at < %s)) "
            "ORDER BY created_at LIMIT %s", (guild_id, now - timedelta(days=30), limit),
        )
        memory_candidates = cursor.fetchall()
    return {
        "schema_version": 1, "captured_at": now, "guild_id": guild_id, "window_start": since,
        "incidents": incidents, "incident_limit": limit, "run_summary": run_summary,
        "slow_runs": slow_runs, "slow_run_steps": steps,
        "memory_summary": memory_summary, "memory_audit_candidates": memory_candidates,
        "notes": ["Latency here is agent-run latency, not necessarily Discord end-to-end latency.",
                  "Memory counters identify audit candidates, not proof a memory is wrong.",
                  "All production access was read-only; reports are local, not bot memory."],
    }


def write_snapshot(snapshot: dict, output: Path, *, root: Path) -> None:
    private = (root / ".local" / "maintenance").resolve()
    destination = output.resolve()
    if not destination.is_relative_to(private):
        raise ValueError("Maintenance reports must stay under .local/maintenance/ (git-ignored).")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(snapshot, file, default=str, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-bundles", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".local/maintenance/latest.json"))
    args = parser.parse_args()
    if args.guild_id <= 0 or not 1 <= args.hours <= 168 or not 1 <= args.limit <= 50:
        parser.error("Require a positive guild ID, 1-168 hours, and 1-50 records.")
    url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("DATABASE_PUBLIC_URL or DATABASE_URL is required (use Railway Postgres service).")
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, options="-c default_transaction_read_only=on -c statement_timeout=10000") as connection:
        snapshot = collect_snapshot(connection, guild_id=args.guild_id, hours=args.hours, limit=args.limit,
                                    include_bundles=args.include_bundles)
    write_snapshot(snapshot, args.output, root=Path.cwd())
    print(json.dumps({"output": str(args.output), "incidents": len(snapshot["incidents"]),
                      "slow_runs": len(snapshot["slow_runs"]), "memory_groups": len(snapshot["memory_summary"])}))


if __name__ == "__main__":
    main()
