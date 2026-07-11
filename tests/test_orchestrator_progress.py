from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nycti.chat.orchestrator import ChatOrchestrator
from nycti.chat.run_state import AgentBudget, ToolOutcome, ToolStatus
from nycti.progress import ResponseProgressPhase


class OrchestratorProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_flow_reports_real_agent_phases(self) -> None:
        turns = [
            _turn(tool_calls=[_tool_call("call-1", "web", '{"query":"current release"}')]),
            _turn(text="Grounded answer."),
        ]
        orchestrator = object.__new__(ChatOrchestrator)
        orchestrator.settings = SimpleNamespace(max_completion_tokens=700)
        orchestrator.llm_client = SimpleNamespace(
            provider_capabilities=SimpleNamespace(name="test"),
            complete_chat_turn=AsyncMock(side_effect=turns),
        )
        orchestrator.tool_runner = _ToolRunner()
        orchestrator.agent_budget = AgentBudget()
        progress = _ProgressRecorder()

        text, _ = await orchestrator.run_chat_with_tools(
            chat_model="test-model",
            messages=[{"role": "user", "content": "What is the current release?"}],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            request_text="What is the current release?",
            metrics=None,
            progress=progress,
        )

        self.assertEqual("Grounded answer.", text)
        self.assertEqual(
            [
                ResponseProgressPhase.MODEL,
                ResponseProgressPhase.TOOLS,
                ResponseProgressPhase.MODEL,
                ResponseProgressPhase.COMPOSING,
            ],
            progress.phases,
        )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.phases: list[ResponseProgressPhase] = []

    async def advance(self, phase: ResponseProgressPhase) -> None:
        self.phases.append(phase)


class _ToolRunner:
    async def run(self, tool_calls: list[object], **_kwargs: object) -> list[ToolOutcome]:
        return [
            ToolOutcome(
                call_id=str(getattr(call, "id")),
                tool_name=str(getattr(call, "name")),
                arguments=str(getattr(call, "arguments")),
                status=ToolStatus.OK,
                content="Current release evidence.",
            )
            for call in tool_calls
        ]


def _tool_call(call_id: str, name: str, arguments: str) -> object:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def _turn(*, text: str = "", tool_calls: list[object] | None = None) -> object:
    return SimpleNamespace(
        text=text,
        raw_text=text,
        usage=SimpleNamespace(
            feature="chat_reply",
            model="test-model",
            provider="test",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.0,
        ),
        tool_calls=tool_calls or [],
        reasoning_content="",
        finish_reason="stop",
        native_tool_calling_failed=False,
        native_tool_failure_request_json="",
    )


if __name__ == "__main__":
    unittest.main()
