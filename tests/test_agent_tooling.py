import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.tool_eligibility import select_eligible_tools
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.registry import TOOL_SPECS
from nycti.chat.tools.schemas import build_chat_tools


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

    def test_tool_eligibility_policy(self) -> None:
        cases = (
            ("latest price for NVDA and SPY", {"quote"}, set()),
            ("summarize https://example.com/press-release", {"url_extract"}, {"web"}),
            (
                "summarize this YouTube video https://youtu.be/dQw4w9WgXcQ",
                {"yt_transcript"},
                {"web"},
            ),
            ("do you think this plan is reasonable?", set(), {"web", "quote", "url_extract"}),
            (
                "give me divident and underlying change percentage by year for jepi; compare with spx",
                {"annual_perf", "python"},
                {"web", "quote", "price_hist"},
            ),
            ("summarize what happened in the channel earlier today", {"channel_ctx"}, set()),
            ("remind me tomorrow at 9am to check the deployment", {"reminder"}, {"web"}),
        )

        for prompt, expected, forbidden in cases:
            with self.subTest(prompt=prompt):
                eligible, _ = select_eligible_tools(
                    request_text=prompt,
                    search_requested=False,
                    guild_id=1,
                )
                self.assertTrue(expected <= eligible)
                self.assertFalse(forbidden & eligible)
                self.assertEqual(bool(expected), bool(eligible))


if __name__ == "__main__":
    unittest.main()
