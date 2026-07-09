from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import unittest

from nycti.chat.run_state import AgentRun, AgentStep, StopReason
from nycti.chat.run_telemetry import AgentRunTelemetryWriter, _serialize_diagnostic_messages


class _Row:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class AgentRunEvent(_Row):
    pass


class AgentStepEvent(_Row):
    pass


class ToolCallEvent(_Row):
    pass


class UsageEvent(_Row):
    pass


class _Session:
    def __init__(self) -> None:
        self.rows: list[object] = []
        self.commits = 0

    def add_all(self, rows: list[object]) -> None:
        self.rows.extend(rows)

    async def commit(self) -> None:
        self.commits += 1


class _Database:
    def __init__(self) -> None:
        self.session_value = _Session()

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class AgentRunTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostic_message_serialization_omits_image_payloads(self) -> None:
        rendered = _serialize_diagnostic_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,secret-image"},
                        },
                    ],
                }
            ]
        )

        self.assertIn("inspect this", rendered)
        self.assertIn("[image omitted]", rendered)
        self.assertNotIn("secret-image", rendered)

    async def test_flush_persists_run_outcome_steps_and_usage_together(self) -> None:
        database = _Database()
        writer = AgentRunTelemetryWriter(database)  # type: ignore[arg-type]
        run = AgentRun(messages=[])
        run.final_status = "fallback"
        run.final_failure_reason = "provider_error"
        run.stop_reason = StopReason.PROVIDER_ERROR
        run.add_step_record(
            state=AgentStep.MODEL,
            feature="chat_reply_provider_attempt",
            status="error",
            provider="clarifai",
        )
        run.usage_records.append(
            SimpleNamespace(
                feature="chat_reply",
                provider="clarifai",
                model="test-model",
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
                estimated_cost_usd=0.0,
            )
        )

        fake_models = ModuleType("nycti.db.models")
        fake_models.AgentRunEvent = AgentRunEvent
        fake_models.AgentStepEvent = AgentStepEvent
        fake_models.ToolCallEvent = ToolCallEvent
        fake_models.UsageEvent = UsageEvent
        with patch.dict("sys.modules", {"nycti.db.models": fake_models}):
            await writer.flush(run, guild_id=1, channel_id=2, user_id=3)

        rows = database.session_value.rows
        run_event = next(row for row in rows if isinstance(row, AgentRunEvent))
        self.assertEqual("fallback", run_event.final_status)
        self.assertEqual("provider_error", run_event.failure_reason)
        self.assertTrue(any(isinstance(row, AgentStepEvent) for row in rows))
        self.assertTrue(any(isinstance(row, UsageEvent) for row in rows))
        self.assertEqual(1, database.session_value.commits)


if __name__ == "__main__":
    unittest.main()
