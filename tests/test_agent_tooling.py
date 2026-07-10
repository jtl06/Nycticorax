import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.orchestrator_support import format_available_tool_guidance
from nycti.chat.tool_eligibility import (
    READ_ONLY_TOOL_NAMES,
    select_answer_plan,
    select_eligible_tools,
)
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.registry import TOOL_SPECS
from nycti.chat.tools.schemas import build_chat_tools

GUILD_PROPOSAL_TOOL_NAMES = {"reminder", "send_msg"}


class AgentTraceTests(unittest.TestCase):
    def test_agent_trace_renders_compact_spans(self) -> None:
        trace = AgentTrace(enabled=True)
        trace.add("tool:web_search", elapsed_ms=123, attrs={"model": "cheap", "empty": ""})

        rendered = trace.render()

        self.assertIn("tool:web_search: 123ms", rendered)
        self.assertIn("model=cheap", rendered)
        self.assertNotIn("empty", rendered)


class ToolRegistryTests(unittest.TestCase):
    def test_all_chat_tools_are_registered(self) -> None:
        names = {
            tool["function"]["name"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        }

        self.assertEqual(names, set(TOOL_SPECS))

    def test_all_registered_handlers_exist_on_executor(self) -> None:
        missing = [
            spec.handler_name
            for spec in TOOL_SPECS.values()
            if not hasattr(ChatToolExecutor, spec.handler_name)
        ]

        self.assertEqual(missing, [])

    def test_tool_promotion_policy_never_restricts_read_eligibility(self) -> None:
        prompts = {
            "latest price for NVDA and SPY": {"quote", "web"},
            "summarize https://example.com/press-release": {"url_extract", "web"},
            "summarize this YouTube video https://youtu.be/dQw4w9WgXcQ": {
                "url_extract",
                "web",
                "yt_transcript",
            },
            "do you think this plan is reasonable?": set(),
            "give me divident and underlying change percentage by year for jepi; compare with spx": {
                "annual_perf",
                "python",
                "url_extract",
                "web",
            },
            "summarize what happened in the channel earlier today": {"channel_ctx"},
        }

        for prompt, expected in prompts.items():
            with self.subTest(prompt=prompt):
                eligible, _ = select_eligible_tools(
                    request_text=prompt,
                    guild_id=1,
                )
                plan, _ = select_answer_plan(request_text=prompt, guild_id=1)
                self.assertEqual(
                    set(READ_ONLY_TOOL_NAMES) | GUILD_PROPOSAL_TOOL_NAMES,
                    eligible,
                )
                self.assertEqual(expected, set(plan.promoted_tool_names))

    def test_action_proposal_tools_are_language_agnostic_and_guild_gated(self) -> None:
        ordinary, _ = select_eligible_tools(
            request_text="How was your day?",
            guild_id=1,
        )
        direct_message, _ = select_eligible_tools(
            request_text="Remind me tomorrow",
            guild_id=None,
        )

        self.assertTrue(GUILD_PROPOSAL_TOOL_NAMES.issubset(ordinary))
        self.assertTrue(GUILD_PROPOSAL_TOOL_NAMES.isdisjoint(direct_message))

    def test_tool_guidance_covers_volatile_company_status(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"web", "quote"})

        self.assertIn("For live/current asks", guidance)
        self.assertIn("how did X do today", guidance)
        self.assertIn("volatile company-status facts", guidance)
        self.assertIn("IPO/public status", guidance)
        self.assertIn("current evidence", guidance)
        self.assertIn("model memory", guidance)
        self.assertIn("For current price asks, use quote", guidance)
        self.assertIn("plausible ticker", guidance)
        self.assertIn("combined public/private valuations", guidance)
        self.assertIn("ignore token pages", guidance)
        self.assertLess(len(guidance), 1800)

    def test_tool_guidance_only_includes_relevant_sections(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"python"})

        self.assertNotIn("current price", guidance)
        self.assertNotIn("investor-relations", guidance)
        self.assertLess(len(guidance), 500)


if __name__ == "__main__":
    unittest.main()
