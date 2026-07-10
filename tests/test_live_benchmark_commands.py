from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from discord import app_commands

from nycti.discord.live_benchmarks import (
    _available_tools_for_case,
    _benchmark_request_context,
    _run_suite,
    _send_benchmark_notice,
    _send_suite_completion,
    build_live_benchmark_attempt_input,
    format_live_benchmark_failures,
    format_live_benchmark_batch_report,
    format_live_benchmark_suite_summary,
    register_live_benchmark_commands,
)
from nycti.live_benchmarks import (
    LiveBenchmarkAttempt,
    LiveBenchmarkCase,
    LiveBenchmarkCheckResult,
    LiveBenchmarkChecks,
    LiveBenchmarkEvaluation,
    LiveBenchmarkExecution,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    LiveBenchmarkSuiteResult,
)


class LiveBenchmarkCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_registers_suite_failure_and_trace_subcommands(self) -> None:
        group = app_commands.Group(name="benchmark", description="Benchmarks")

        register_live_benchmark_commands(SimpleNamespace(), group)

        self.assertEqual(
            {"suite", "failures", "trace"},
            {command.name for command in group.commands},
        )

    def test_attempt_input_keeps_summary_and_rich_failure_trace(self) -> None:
        attempt = _attempt(status=LiveBenchmarkStatus.FAIL)

        record = build_live_benchmark_attempt_input(attempt, manifest_version=3)

        self.assertEqual("3", record.suite_version)
        self.assertEqual("fixture-web", record.case_id)
        self.assertEqual("fail", record.status)
        self.assertEqual(("web",), record.tools_called)
        self.assertEqual("deepseek-ai/DeepSeek-V4-Pro", record.model)
        self.assertEqual("deepinfra", record.provider)
        self.assertEqual(153, record.total_tokens)
        assert record.failure_artifact is not None
        self.assertEqual("Latest LumenOS?", record.failure_artifact["prompt"])
        self.assertIn("metrics", record.failure_artifact)
        self.assertIn("diagnostic_agent_messages_json", record.failure_artifact)
        self.assertIn("diagnostic_agent_steps_json", record.failure_artifact)
        self.assertIn("tool_schemas_json", record.failure_artifact)

    def test_suite_summary_identifies_durable_failure_rows(self) -> None:
        attempt = _attempt(status=LiveBenchmarkStatus.FAIL)
        result = LiveBenchmarkSuiteResult(
            batch_id="abcdef1234567890",
            manifest_version=1,
            mode=LiveBenchmarkMode.FIXTURES,
            attempts=(attempt,),
            started_at=datetime.now(UTC),
            latency_ms=1540,
        )

        rendered = format_live_benchmark_suite_summary(
            result,
            stored_ids={attempt.attempt_id: 42},
        )

        self.assertIn("1 fail", rendered)
        self.assertIn("log `#42`", rendered)
        self.assertIn("/benchmark trace", rendered)
        report = format_live_benchmark_batch_report(
            result,
            stored_ids={attempt.attempt_id: 42},
        )
        self.assertIn("# Nycti Live LLM Benchmark", report)
        self.assertIn("| `fixture-web` | 1 | FAIL", report)
        self.assertIn("deepseek-ai/DeepSeek-V4-Pro", report)
        self.assertIn("deepinfra", report)
        self.assertIn("web", report)
        self.assertIn("| 2 | 153 | final_text | 42 | 0.4s |", report)
        self.assertIn("fixture failure", report)

    def test_failure_list_is_bounded_and_points_to_trace_command(self) -> None:
        failures = [
            SimpleNamespace(
                id=index,
                status="fail",
                case_id=f"case-{index}-" + "x" * 120,
                attempt_index=1,
                score=1.0,
                max_score=3.0,
                latency_ms=100,
                model="model-" + "y" * 120,
            )
            for index in range(30)
        ]

        rendered = format_live_benchmark_failures(failures)

        self.assertLessEqual(len(rendered), 1950)
        self.assertIn("Recent live-benchmark failures", rendered)

    def test_available_tools_switch_between_fixture_and_production_runners(self) -> None:
        fixture = SimpleNamespace(
            executor=_AvailabilityExecutor({"web", "python"})
        )
        production = SimpleNamespace(
            executor=_AvailabilityExecutor({"web", "quote"})
        )
        bot = SimpleNamespace(
            _chat_orchestrator=SimpleNamespace(tool_runner=production)
        )

        fixture_names = _available_tools_for_case(
            bot,
            fixture_tool_runner=fixture,
            case=_case(LiveBenchmarkMode.FIXTURES),
        )
        canary_names = _available_tools_for_case(
            bot,
            fixture_tool_runner=fixture,
            case=_case(LiveBenchmarkMode.CANARIES),
        )

        self.assertEqual({"web", "python"}, set(fixture_names or ()))
        self.assertEqual({"web", "quote"}, set(canary_names or ()))

    async def test_request_context_rejects_dm_with_awaited_response(self) -> None:
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            channel=None,
            user=SimpleNamespace(id=1),
            guild=None,
            response=response,
        )

        self.assertIsNone(await _benchmark_request_context(interaction))
        response.send_message.assert_awaited_once()

    async def test_expired_interaction_delivers_batch_report_to_channel(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            is_expired=lambda: True,
            channel=channel,
            user=SimpleNamespace(id=42),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await _send_suite_completion(
            interaction,
            summary="Batch complete.",
            report="# report\n",
        )

        interaction.followup.send.assert_not_awaited()
        channel.send.assert_awaited_once()
        self.assertIn("<@42>", channel.send.await_args.args[0])
        self.assertEqual(
            "nycti-live-benchmark-results.md",
            channel.send.await_args.kwargs["file"].filename,
        )

    async def test_expired_interaction_delivers_late_notice_to_channel(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            is_expired=lambda: True,
            channel=channel,
            user=SimpleNamespace(id=42),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await _send_benchmark_notice(interaction, "Cancelled safely.")

        interaction.followup.send.assert_not_awaited()
        channel.send.assert_awaited_once()
        self.assertIn("<@42> Cancelled safely.", channel.send.await_args.args[0])

    async def test_suite_runs_real_reply_path_isolated_and_persists_attempt(self) -> None:
        generate_reply = AsyncMock(
            return_value=(
                "568826903",
                {
                    "agent_run_id": "run-live-1",
                    "active_chat_model": "DeepSeek-V4-Pro",
                    "active_chat_provider": "deepinfra",
                    "routing_called_tools": "python",
                    "routing_successful_tools": "python",
                    "agent_tool_call_count": 1,
                    "agent_model_turn_count": 2,
                    "reply_generation_ms": 1_000,
                    "agent_total_tokens": 1_000,
                    "agent_stop_reason": "final_text",
                },
            )
        )
        bot = SimpleNamespace(
            _generate_reply=generate_reply,
            _chat_orchestrator=SimpleNamespace(tool_runner=SimpleNamespace()),
            database=SimpleNamespace(),
        )

        with patch(
            "nycti.discord.live_benchmarks.save_live_benchmark_attempt",
            new=AsyncMock(return_value=77),
        ) as save_attempt:
            result, stored_ids = await _run_suite(
                bot,
                mode="fixtures",
                case_id="fixture-calculation",
                repeats=1,
            )

        self.assertEqual(1, result.count(LiveBenchmarkStatus.PASS))
        self.assertEqual({result.attempts[0].attempt_id: 77}, stored_ids)
        generate_kwargs = generate_reply.await_args.kwargs
        self.assertTrue(generate_kwargs["isolated_benchmark"])
        self.assertIsNotNone(generate_kwargs["isolated_benchmark_now"])
        self.assertFalse(generate_kwargs["persist_memory"])
        self.assertFalse(generate_kwargs["include_memories"])
        self.assertGreater(generate_kwargs["user_id"], 0)
        self.assertIsNotNone(generate_kwargs["tool_runner"])
        save_attempt.assert_awaited_once()


class _AvailabilityExecutor:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def available_tool_names(self, **_kwargs: object) -> set[str]:
        return self.names


def _case(mode: LiveBenchmarkMode = LiveBenchmarkMode.FIXTURES) -> LiveBenchmarkCase:
    return LiveBenchmarkCase(
        case_id="fixture-web",
        mode=mode,
        prompt="Latest LumenOS?",
        description="Freshness fixture",
        checks=LiveBenchmarkChecks(required_tools=("web",)),
    )


def _attempt(*, status: LiveBenchmarkStatus) -> LiveBenchmarkAttempt:
    execution = LiveBenchmarkExecution(
        answer="LumenOS 7.4 [S1].",
        metrics={
            "agent_run_id": "run-1",
            "active_chat_model": "deepseek-ai/DeepSeek-V4-Pro",
            "active_chat_provider": "deepinfra",
            "answer_profile": "grounded",
            "chat_prompt_tokens": 100,
            "chat_completion_tokens": 23,
            "chat_total_tokens": 123,
            "agent_total_tokens": 153,
            "agent_model_turn_count": 2,
            "agent_stop_reason": "final_text",
            "routing_called_tools": "web",
            "_diagnostic_agent_messages_json": "[]",
        },
    )
    evaluation = LiveBenchmarkEvaluation(
        status=status,
        checks=(
            LiveBenchmarkCheckResult(
                check_id="tool:called:web",
                passed=False,
                detail="fixture failure",
            ),
        ),
    )
    return LiveBenchmarkAttempt(
        batch_id="batch-1",
        case=_case(),
        attempt_index=1,
        evaluation=evaluation,
        started_at=datetime.now(UTC),
        latency_ms=400,
        execution=execution,
    )


if __name__ == "__main__":
    unittest.main()
