import unittest
import ast
from pathlib import Path
from nycti.chat.tool_fallback import fallback_tool_result


class ChatOrchestratorTests(unittest.TestCase):
    def test_fallback_tool_result_does_not_dump_raw_channel_context(self) -> None:
        result = fallback_tool_result(
            "Older Discord channel context (raw, oldest to newest):\n"
            "[2026-04-13 04:00 UTC] mat: one\n"
            "[2026-04-13 04:01 UTC] lucis: two"
        )

        self.assertIn("failed to synthesize", result)
        self.assertNotIn("mat: one", result)

    def test_fallback_tool_result_keeps_non_context_results(self) -> None:
        self.assertEqual(fallback_tool_result("Market quote failed."), "Market quote failed.")

    def test_fallback_tool_result_sanitizes_raw_tavily_web_dump(self) -> None:
        result = fallback_tool_result(
            "Tavily web results for: nvda earnings\n\n1. Headline\nhttps://example.com\nsnippet"
        )
        self.assertIn("couldn't synthesize", result)
        self.assertNotIn("Tavily web results for:", result)

    def test_orchestrator_has_tool_answer_rewrite_gating(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()
        tree = ast.parse(source)
        chat_orchestrator = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "ChatOrchestrator"
            ),
            None,
        )
        self.assertIsNotNone(chat_orchestrator)
        assert chat_orchestrator is not None

        method = next(
            (
                node
                for node in chat_orchestrator.body
                if isinstance(node, ast.FunctionDef) and node.name == "_should_run_tool_answer_rewrite"
            ),
            None,
        )
        self.assertIsNotNone(method)
        assert method is not None

        attrs = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
        }
        self.assertIn("tool_answer_rewrite_enabled", attrs)
        self.assertIn("tool_answer_rewrite_min_chars", attrs)

        names = {
            node.id
            for node in ast.walk(method)
            if isinstance(node, ast.Name)
        }
        self.assertIn("used_tools", names)

    def test_orchestrator_has_tool_planner_and_synthesis_paths(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("chat_tool_plan", source)
        self.assertIn("TOOL_PLANNER_CONTEXT_CHAR_LIMIT", source)
        self.assertIn("_format_tool_plan_guidance", source)
        self.assertIn("chat_reply_synthesis", source)
        self.assertIn("_format_tool_evidence", source)
        self.assertIn("EVIDENCE_TOOL_NAMES", source)

    def test_orchestrator_breaks_repeated_tool_calls_and_avoids_limit_message(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("seen_tool_call_signatures", source)
        self.assertIn("_tool_call_signature", source)
        self.assertIn("You already made those exact tool calls.", source)
        self.assertNotIn("I hit the tool-call limit for this reply.", source)

    def test_orchestrator_dynamically_selects_exposed_tools(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("_select_exposed_tool_names", source)
        self.assertIn("_heuristic_tool_names", source)
        self.assertIn("_current_request_excerpt", source)
        self.assertIn("exposed_tool_count", source)
        self.assertIn("build_chat_tools(selected_tool_names)", source)


if __name__ == "__main__":
    unittest.main()
