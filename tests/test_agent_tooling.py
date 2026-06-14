import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.tool_eligibility import READ_ONLY_TOOL_NAMES, select_eligible_tools
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
        prompts = (
            "latest price for NVDA and SPY",
            "summarize https://example.com/press-release",
            "summarize this YouTube video https://youtu.be/dQw4w9WgXcQ",
            "do you think this plan is reasonable?",
            "give me divident and underlying change percentage by year for jepi; compare with spx",
            "summarize what happened in the channel earlier today",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                eligible, _ = select_eligible_tools(
                    request_text=prompt,
                    search_requested=False,
                    guild_id=1,
                )
                self.assertEqual(set(READ_ONLY_TOOL_NAMES), eligible)

    def test_action_tools_remain_intent_gated(self) -> None:
        ordinary, permissions = select_eligible_tools(
            request_text="How was your day?",
            search_requested=False,
            guild_id=1,
        )

        self.assertNotIn("reminder", ordinary)
        self.assertNotIn("send_msg", ordinary)
        self.assertFalse(permissions.allow_reminders)
        self.assertFalse(permissions.allow_cross_channel_send)


if __name__ == "__main__":
    unittest.main()
