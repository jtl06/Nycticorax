from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.chat.model_runner import _reasoning_effort_override
from nycti.chat.orchestrator_support import (
    agent_run_output_budgets,
    output_budget_for_run,
)
from nycti.chat.run_state import AgentBudget, AgentRun, AnswerPlan, AnswerProfile


class PostToolPolicyTests(unittest.TestCase):
    def test_grounded_reasoning_stays_at_configured_effort_after_successful_tool(self) -> None:
        run = _run(AnswerProfile.GROUNDED, override=None)

        self.assertIsNone(_reasoning_effort_override(run))

        run.attempted_tools.add("web")
        self.assertIsNone(_reasoning_effort_override(run))

        run.successful_tools.add("web")
        self.assertIsNone(_reasoning_effort_override(run))

    def test_deep_reasoning_stays_high_after_successful_tool(self) -> None:
        run = _run(AnswerProfile.DEEP, override="high")
        run.successful_tools.add("deep_research")

        self.assertEqual("high", _reasoning_effort_override(run))

    def test_grounded_post_tool_budget_keeps_high_reasoning_reserve(self) -> None:
        metrics: dict[str, int | str] = {}
        initial, post_tool = agent_run_output_budgets(
            SimpleNamespace(max_completion_tokens=700),
            answer_profile=AnswerProfile.GROUNDED,
            hidden_reasoning_effort="high",
            metrics=metrics,
        )
        run = _run(AnswerProfile.GROUNDED, override=None)

        self.assertEqual(4096, initial.reply_tokens)
        self.assertIs(initial, post_tool)
        self.assertIs(initial, output_budget_for_run(run, initial=initial, post_tool=post_tool))

        run.successful_tools.add("web")
        self.assertIs(post_tool, output_budget_for_run(run, initial=initial, post_tool=post_tool))
        self.assertNotIn("answer_post_tool_followup_token_budget", metrics)


def _run(profile: AnswerProfile, *, override: str | None) -> AgentRun:
    budget = AgentBudget()
    return AgentRun(
        messages=[],
        budget=budget,
        answer_plan=AnswerPlan(
            profile=profile,
            eligible_tool_names=frozenset(),
            budget=budget,
            reasoning_effort_override=override,
        ),
    )


if __name__ == "__main__":
    unittest.main()
