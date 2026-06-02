import unittest
import ast
from pathlib import Path
from nycti.chat.tool_fallback import fallback_tool_result


def _orchestrator_sources() -> str:
    return (
        Path("src/nycti/chat/orchestrator.py").read_text()
        + "\n"
        + Path("src/nycti/chat/orchestrator_support.py").read_text()
    )


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

    def test_fallback_tool_result_sanitizes_raw_youtube_transcript(self) -> None:
        result = fallback_tool_result(
            "YouTube transcript for: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n[0:00] transcript text"
        )
        self.assertIn("couldn't synthesize", result)
        self.assertNotIn("[0:00] transcript text", result)

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

    def test_orchestrator_has_tool_synthesis_paths(self) -> None:
        source = _orchestrator_sources()

        self.assertIn("chat_reply_synthesis", source)
        self.assertIn("_format_tool_evidence", source)
        self.assertIn("EVIDENCE_TOOL_NAMES", source)

    def test_orchestrator_breaks_repeated_tool_calls_and_avoids_limit_message(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("seen_tool_call_signatures", source)
        self.assertIn("_tool_call_signature", source)
        self.assertIn("You already made those exact tool calls.", source)
        self.assertNotIn("I hit the tool-call limit for this reply.", source)

    def test_orchestrator_exposes_all_tools_without_planner_or_regex_router(self) -> None:
        source = _orchestrator_sources()

        self.assertIn("ACTION_TOOL_NAMES", source)
        self.assertIn("tools = build_chat_tools()", source)
        self.assertIn("available_tool_names = _tool_names(tools)", source)
        self.assertNotIn("_select_exposed_tool_names", source)
        self.assertNotIn("_safety_tool_overrides", source)
        self.assertNotIn("_looks_like_live_market_request", source)
        self.assertNotIn("_looks_like_market_news_request", source)
        self.assertNotIn("_maybe_plan_tool_use", source)
        self.assertNotIn("chat_tool_plan", source)
        self.assertNotIn("build_tool_planner_catalog", source)
        self.assertNotIn("TOOL_PLANNER_CONTEXT_CHAR_LIMIT", source)
        self.assertNotIn("format_tool_plan_guidance", source)
        self.assertNotIn("tools_to_try", source)
        self.assertIn("exposed_tool_count", source)
        self.assertIn("missing_required_tools = required_tools - used_tools", source)
        self.assertNotIn("expose_tools", source)

    def test_orchestrator_keeps_general_tool_grounding_guidance(self) -> None:
        source = _orchestrator_sources()

        self.assertIn("Available tools this turn", source)
        self.assertIn("Do not write textual or XML tool-call markup", source)
        self.assertIn("Native tool schemas are unavailable", source)
        self.assertIn("historical benchmark", source)
        self.assertIn("Do not answer historical", source)
        self.assertIn("current local date/time", source)
        self.assertIn("could have changed", source)

    def test_orchestrator_continues_length_limited_answers(self) -> None:
        source = _orchestrator_sources()

        self.assertIn("_should_continue_answer(initial_turn", source)
        self.assertIn("chat_reply_continuation", source)
        self.assertIn("MAX_LENGTH_CONTINUATION_ROUNDS", source)
        self.assertIn("MIN_CHAT_REPLY_COMPLETION_TOKENS", source)
        self.assertIn("TOOL_SYNTHESIS_TOKEN_DIVISOR = 4", source)
        self.assertIn("_tool_synthesis_max_tokens(self.settings)", source)
        self.assertIn("LENGTH_CONTINUATION_TOKEN_MARGIN", source)
        self.assertIn("looks_structurally_incomplete_answer", source)
        self.assertIn("turn.usage.completion_tokens", source)
        self.assertIn("chat_length_finish_count", source)
        self.assertIn("chat_continuation_count", source)

    def test_orchestrator_avoids_hardcoded_regex_routing(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()
        tree = ast.parse(source)

        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_from_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        re_attribute_uses = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "re"
        ]

        self.assertNotIn("re", imported_modules)
        self.assertNotIn("re", imported_from_modules)
        self.assertEqual([], re_attribute_uses)
        self.assertNotIn("BARE_TICKER_RE", source)
        self.assertNotIn("MARKET_RECORD_TERMS", source)
        self.assertNotIn("_looks_like_market_record_request", source)


if __name__ == "__main__":
    unittest.main()
