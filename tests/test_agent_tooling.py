import unittest

from nycti.agent_eval import load_agent_eval_cases, validate_agent_eval_cases
from nycti.agent_trace import AgentTrace
from nycti.chat.tools.mcp_adapter import build_mcp_tool_descriptors
from nycti.chat.tools.registry import TOOL_METADATA, build_tool_planner_catalog
from nycti.chat.tools.schemas import STOCK_QUOTE_TOOL_NAME, WEB_SEARCH_TOOL_NAME, build_chat_tools


class AgentTraceTests(unittest.TestCase):
    def test_agent_trace_renders_compact_spans(self) -> None:
        trace = AgentTrace(enabled=True)
        trace.add("tool:web_search", elapsed_ms=123, attrs={"model": "cheap", "empty": ""})

        rendered = trace.render()

        self.assertIn("tool:web_search: 123ms", rendered)
        self.assertIn("model=cheap", rendered)
        self.assertNotIn("empty", rendered)


class ToolRegistryTests(unittest.TestCase):
    def test_all_chat_tools_have_metadata(self) -> None:
        names = {
            tool["function"]["name"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        }

        self.assertEqual(names, set(TOOL_METADATA))

    def test_tool_planner_catalog_contains_risk_and_env(self) -> None:
        catalog = build_tool_planner_catalog({WEB_SEARCH_TOOL_NAME, STOCK_QUOTE_TOOL_NAME})

        self.assertIn("web_search", catalog)
        self.assertIn("risk=", catalog)
        self.assertIn("TAVILY_API_KEY", catalog)
        self.assertIn("TWELVE_DATA_API_KEY", catalog)


class MCPAdapterTests(unittest.TestCase):
    def test_build_mcp_tool_descriptors_uses_input_schema_and_annotations(self) -> None:
        descriptors = build_mcp_tool_descriptors()
        descriptor = next(item for item in descriptors if item["name"] == WEB_SEARCH_TOOL_NAME)

        self.assertIn("inputSchema", descriptor)
        self.assertEqual(descriptor["annotations"]["nycti/skill"], "fresh_web")
        self.assertIn("nycti/risk", descriptor["annotations"])


class AgentEvalTests(unittest.TestCase):
    def test_agent_eval_cases_are_valid(self) -> None:
        cases = load_agent_eval_cases("tests/agent_eval_cases.json")
        errors = validate_agent_eval_cases(cases)

        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
