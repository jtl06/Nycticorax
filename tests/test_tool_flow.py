import unittest

from nycti.chat.run_state import AgentRun
from nycti.chat.tool_flow import record_executable_tool_calls, select_fresh_tool_calls
from nycti.llm.tool_calls import LLMToolCall


class ToolSelectionTests(unittest.TestCase):
    def test_only_executed_calls_enter_duplicate_history(self) -> None:
        run = AgentRun(messages=[])
        first = LLMToolCall(id="one", name="web", arguments='{"query":"earnings"}')
        duplicate = LLMToolCall(id="two", name="web", arguments=first.arguments)
        metrics = {}

        def select(calls):
            return select_fresh_tool_calls(
                run, calls, available_tool_names={"web"}, metrics=metrics,
            )

        self.assertEqual([first], select([first, duplicate]))
        self.assertEqual(set(), run.seen_tool_signatures)
        # A call skipped by the budget remains eligible on a later turn.
        self.assertEqual([first], select([first]))
        record_executable_tool_calls(run, [first])
        self.assertEqual([], select([duplicate]))

    def test_similar_queries_can_refine_dates_and_subjects(self) -> None:
        run = AgentRun(messages=[])
        first = LLMToolCall(id="one", name="web", arguments='{"query":"NVIDIA earnings 2025"}')
        record_executable_tool_calls(run, [first])
        refinement = LLMToolCall(id="two", name="web", arguments='{"query":"NVIDIA earnings 2026"}')
        self.assertEqual([refinement], select_fresh_tool_calls(
            run, [refinement], available_tool_names={"web"}, metrics={},
        ))
