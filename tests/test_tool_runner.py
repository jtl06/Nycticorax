from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tool_runner import ToolRunner


class ToolRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_execution_preserves_partial_success(self) -> None:
        runner = ToolRunner(_MixedExecutor())

        outcomes = await runner.run(
            [_call("ok", "web", "{}"), _call("bad", "quote", "{}")],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertEqual([ToolStatus.OK, ToolStatus.ERROR], [outcome.status for outcome in outcomes])
        self.assertIn("RuntimeError", outcomes[1].content)

    async def test_tool_outcome_carries_latency_metrics_and_provenance(self) -> None:
        runner = ToolRunner(_ProvenanceExecutor())

        outcomes = await runner.run(
            [_call("one", "web", '{"query":"earnings"}')],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertGreaterEqual(outcomes[0].latency_ms, 0)
        self.assertEqual(("https://investor.example.com/results",), outcomes[0].provenance)
        self.assertEqual({"web_search_ms": 3}, outcomes[0].metrics)

    async def test_empty_extract_uses_registry_fallback_guidance(self) -> None:
        runner = ToolRunner(_EmptyExtractExecutor())

        outcomes = await runner.run(
            [_call("one", "url_extract", '{"url":"https://example.com/guessed"}')],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            permissions=AgentPermissions(),
            run_id="test-run",
            step_index=1,
        )

        self.assertEqual(ToolStatus.EMPTY, outcomes[0].status)
        self.assertIn("use web search to locate the exact source URL", outcomes[0].content)
class _MixedExecutor:
    async def execute(self, *, tool_name: str, **_kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        if tool_name == "quote":
            raise RuntimeError("provider down")
        return ToolExecutionResult(
            content="Useful result.",
            status=ToolStatus.OK,
            metrics={"web_search_ms": 1},
        )


class _EmptyExtractExecutor:
    async def execute(self, **_kwargs):  # type: ignore[no-untyped-def]
        return ToolExecutionResult(
            content="No extractable content found for: https://example.com/guessed",
            status=ToolStatus.EMPTY,
        )


class _ProvenanceExecutor:
    async def execute(self, **_kwargs):  # type: ignore[no-untyped-def]
        return ToolExecutionResult(
            content="Official result: https://investor.example.com/results",
            status=ToolStatus.OK,
            metrics={"web_search_ms": 3},
            provenance=("https://investor.example.com/results",),
        )

def _call(call_id: str, name: str, arguments: str) -> object:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


if __name__ == "__main__":
    unittest.main()
