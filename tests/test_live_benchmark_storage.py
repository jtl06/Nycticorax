from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import unittest
from typing import Any, cast

from sqlalchemy import Table, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nycti.db.models import Base, LiveBenchmarkAttemptRecord
from nycti.live_benchmark_storage import (
    FAILURE_ARTIFACT_BYTE_LIMIT,
    LIVE_BENCHMARK_RETENTION,
    LiveBenchmarkAttemptInput,
    build_live_benchmark_attempt_record,
    get_live_benchmark_failure_artifact,
    list_recent_live_benchmark_failures,
    prune_expired_live_benchmark_attempts,
    save_live_benchmark_attempt,
    serialize_live_benchmark_failure_artifact,
)


class LiveBenchmarkStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = await _TestDatabase.create()

    async def asyncTearDown(self) -> None:
        await self.database.close()

    async def test_save_commits_complete_attempt_without_identity_columns(self) -> None:
        created_at = datetime.now(timezone.utc)
        attempt_id = await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(created_at=created_at),
        )

        async with self.database.session() as session:
            row = await session.get(LiveBenchmarkAttemptRecord, attempt_id)

        assert row is not None
        self.assertEqual(row.batch_id, "batch-1")
        self.assertEqual(row.case_id, "short-current-answer")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.score, 4.0)
        self.assertEqual(row.max_score, 4.0)
        self.assertEqual(row.model, "economy-model")
        self.assertEqual(row.provider, "openai")
        self.assertEqual(row.profile, "quick")
        self.assertEqual(row.prompt_tokens, 12)
        self.assertEqual(row.completion_tokens, 8)
        self.assertEqual(row.total_tokens, 20)
        self.assertEqual(row.latency_ms, 321)
        self.assertEqual(row.tools_called, ["web_search"])
        self.assertIsNone(row.failure_artifact_json)
        self.assertEqual(
            _as_utc(row.expires_at),
            created_at + LIVE_BENCHMARK_RETENTION,
        )
        column_names = {column.name for column in row.__table__.columns}
        self.assertFalse(
            column_names
            & {"guild_id", "channel_id", "user_id", "source_message_id"}
        )

    async def test_failures_store_redacted_sanitized_trace_but_passes_do_not(self) -> None:
        diagnostic_messages = json.dumps(
            [
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                {"type": "reasoning", "content": "private chain of thought"},
                {
                    "type": "output_text",
                    "text": "usable trace",
                    "previous_response_id": "resp_private",
                },
            ]
        )
        failure_id = await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(
                status="FAIL",
                score=2,
                failed_checks=("called web_search",),
                error="Authorization: Bearer top.secret",
                failure_artifact={
                    "prompt": "Latest model? token=my-token",
                    "answer": "I do not know.",
                    "guild_id": 991,
                    "metrics": {
                        "channel_id": 992,
                        "provider_recovery_request_json": '{"password":"secret"}',
                        "chat_total_tokens": 20,
                        "_diagnostic_agent_steps_json": json.dumps(
                            [
                                {
                                    "provider": "deepinfra",
                                    "active_model": "DeepSeek-V4-Pro",
                                    "tool_name": "web",
                                    "user_id": 77,
                                }
                            ]
                        ),
                    },
                    "diagnostic_agent_messages_json": diagnostic_messages,
                    "responses_output_items": [{"encrypted_content": "opaque"}],
                    "hidden_reasoning": "do not retain",
                },
            ),
        )
        pass_id = await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(
                batch_id="batch-2",
                failure_artifact={"prompt": "must not be stored"},
            ),
        )

        async with self.database.session() as session:
            failed = await session.get(LiveBenchmarkAttemptRecord, failure_id)
            passed = await session.get(LiveBenchmarkAttemptRecord, pass_id)

        assert failed is not None and failed.failure_artifact_json is not None
        payload = json.loads(failed.failure_artifact_json)
        rendered = failed.failure_artifact_json
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["metrics"]["chat_total_tokens"], 20)
        steps = json.loads(payload["metrics"]["_diagnostic_agent_steps_json"])
        self.assertEqual("deepinfra", steps[0]["provider"])
        self.assertEqual("web", steps[0]["tool_name"])
        self.assertNotIn("user_id", steps[0])
        self.assertNotIn("guild_id", payload)
        self.assertNotIn("my-token", rendered)
        self.assertNotIn("top.secret", failed.error or "")
        self.assertNotIn("private chain of thought", rendered)
        self.assertNotIn("data:image", rendered)
        self.assertNotIn("resp_private", rendered)
        self.assertNotIn("opaque", rendered)
        messages = json.loads(payload["diagnostic_agent_messages_json"])
        self.assertIn("usable trace", json.dumps(messages))
        assert passed is not None
        self.assertIsNone(passed.failure_artifact_json)

    async def test_failure_artifact_is_capped_and_remains_valid_json(self) -> None:
        noisy_metrics = {f"trace_{index}": "c" * 20_000 for index in range(100)}
        diagnostic_messages = json.dumps(
            [
                {
                    "role": "tool",
                    "name": "web",
                    "content": f"useful message trace {index} " + "m" * 20_000,
                }
                for index in range(96)
            ]
        )
        diagnostic_steps = json.dumps(
            [
                {
                    "step_index": index,
                    "provider": "deepinfra",
                    "tool_name": "web",
                    "status": "ok",
                    "details": "s" * 4_000,
                }
                for index in range(128)
            ]
        )
        tool_schemas = json.dumps(
            [
                {
                    "name": f"tool_{index}",
                    "description": "d" * 8_000,
                }
                for index in range(64)
            ]
        )
        rendered = serialize_live_benchmark_failure_artifact(
            {
                "prompt": "Short?",
                "answer": "a" * 180_000,
                "agent_trace": "b" * 180_000,
                "metrics": noisy_metrics,
                "diagnostic_agent_messages_json": diagnostic_messages,
                "diagnostic_agent_steps_json": diagnostic_steps,
                "tool_schemas_json": tool_schemas,
                "escaped": ("\U0001f989\n\t\\\"") * 80_000,
            }
        )

        self.assertLessEqual(
            len(rendered.encode("utf-8")),
            FAILURE_ARTIFACT_BYTE_LIMIT,
        )
        payload = json.loads(rendered)
        self.assertTrue(payload.get("artifact_truncated"))
        trace_payload = payload.get("artifact", payload)
        self.assertIn("agent_trace", trace_payload)
        self.assertIn("diagnostic_agent_messages_json", trace_payload)
        self.assertIn("diagnostic_agent_steps_json", trace_payload)
        self.assertIn("tool_schemas_json", trace_payload)
        for field in (
            "diagnostic_agent_messages_json",
            "diagnostic_agent_steps_json",
            "tool_schemas_json",
        ):
            decoded = json.loads(trace_payload[field])
            self.assertIsInstance(decoded, (list, dict), field)

    async def test_recent_failure_listing_and_trace_lookup_are_bounded(self) -> None:
        now = datetime.now(timezone.utc)
        older_id = await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(
                batch_id="older",
                status="fail",
                created_at=now - timedelta(minutes=2),
                failure_artifact={"prompt": "Old?"},
            ),
        )
        newer_id = await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(
                batch_id="newer",
                status="error",
                created_at=now - timedelta(minutes=1),
                error="provider unavailable",
                failure_artifact={"prompt": "New?"},
            ),
        )
        await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(batch_id="passing", created_at=now),
        )

        failures = await list_recent_live_benchmark_failures(
            self.database,
            limit=1,
            now=now,
        )

        self.assertEqual([item.id for item in failures], [newer_id])
        self.assertEqual(failures[0].status, "error")
        self.assertEqual(failures[0].tools_called, ("web_search",))
        trace = await get_live_benchmark_failure_artifact(
            self.database,
            attempt_id=older_id,
            now=now,
        )
        assert trace is not None
        self.assertEqual(json.loads(trace)["prompt"], "Old?")
        self.assertIsNone(
            await get_live_benchmark_failure_artifact(
                self.database,
                attempt_id=999_999,
                now=now,
            )
        )

    async def test_prune_removes_only_expired_attempts(self) -> None:
        now = datetime.now(timezone.utc)
        await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(
                batch_id="expired",
                created_at=now - LIVE_BENCHMARK_RETENTION,
            ),
        )
        await save_live_benchmark_attempt(
            self.database,
            attempt=_attempt(batch_id="retained", created_at=now),
        )

        async with self.database.session() as session:
            deleted = await prune_expired_live_benchmark_attempts(session, now=now)
            await session.commit()

        self.assertEqual(deleted, 1)
        async with self.database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(LiveBenchmarkAttemptRecord)
            )
        self.assertEqual(count, 1)

    def test_invalid_status_attempt_index_and_nonfinite_scores_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "status"):
            build_live_benchmark_attempt_record(_attempt(status="maybe"))
        with self.assertRaisesRegex(ValueError, "attempt_index"):
            build_live_benchmark_attempt_record(_attempt(attempt_index=0))
        with self.assertRaisesRegex(ValueError, "finite"):
            build_live_benchmark_attempt_record(_attempt(score=float("nan")))

    def test_table_has_idempotency_constraint_and_query_indexes(self) -> None:
        table = cast(Table, LiveBenchmarkAttemptRecord.__table__)
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        index_names = {index.name for index in table.indexes}

        self.assertIn(
            ("batch_id", "case_id", "attempt_index"),
            unique_columns,
        )
        self.assertIn("ix_live_benchmark_case_status_created", index_names)
        self.assertIn("ix_live_benchmark_status_created", index_names)
        self.assertIn("ix_live_benchmark_attempts_expires_at", index_names)


def _attempt(**overrides) -> LiveBenchmarkAttemptInput:
    values: dict[str, Any] = {
        "batch_id": "batch-1",
        "suite_version": "2026-07-10",
        "case_id": "short-current-answer",
        "attempt_index": 1,
        "mode": "fixtures",
        "status": "pass",
        "score": 4.0,
        "max_score": 4.0,
        "agent_run_id": "run-1",
        "model": "economy-model",
        "provider": "openai",
        "profile": "quick",
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
        "latency_ms": 321,
        "tools_called": ("web_search",),
        "created_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return LiveBenchmarkAttemptInput(**values)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _TestDatabase:
    def __init__(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    async def create(cls) -> "_TestDatabase":
        database = cls()
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return database

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()


if __name__ == "__main__":
    unittest.main()
