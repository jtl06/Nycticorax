import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from nycti.chat.tools.schemas import WEB_SEARCH_TOOL_NAME
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

    def test_orchestrator_supports_fast_search_early_final(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("fast_search_requested", source)
        self.assertIn("fast_search_early_final_count", source)
        self.assertIn("<= EVIDENCE_TOOL_NAMES", source)
        self.assertIn("_force_final_answer", source)

    def test_orchestrator_combines_evidence_synthesis_and_followup(self) -> None:
        source = Path("src/nycti/chat/orchestrator.py").read_text()

        self.assertIn("_run_evidence_followup", source)
        self.assertIn("Choose exactly one path", source)
        self.assertIn("chat_reply_evidence", source)
        self.assertIn("evidence_tools = build_chat_tools(EVIDENCE_TOOL_NAMES)", source)
        self.assertIn("self._build_evidence_followup_messages", source)
        self.assertIn("*base_messages", source)

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


class ChatOrchestratorBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_followup_answers_without_separate_synthesis_or_final(self) -> None:
        try:
            from nycti.chat.orchestrator import ChatOrchestrator
        except (ImportError, ModuleNotFoundError) as exc:
            self.skipTest(f"Optional bot runtime dependency is not installed or compatible: {exc}")

        fake_llm = _FakeLoopLLM(
            [
                _chat_turn(
                    feature="chat_reply",
                    text="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            name=WEB_SEARCH_TOOL_NAME,
                            arguments='{"query":"NVIDIA AMD earnings"}',
                        )
                    ],
                ),
                _chat_turn(feature="chat_reply_evidence", text="Synthesized earnings comparison."),
            ],
            synthesis_text="Synthesized earnings comparison.",
        )
        orchestrator = _build_test_orchestrator(ChatOrchestrator, fake_llm)
        metrics: dict[str, int | str] = {}

        text, _reasoning = await orchestrator.run_chat_with_tools(
            chat_model="chat-model",
            messages=[{"role": "user", "content": "Compare earnings."}],
            guild_id=None,
            channel_id=None,
            user_id=1,
            source_message_id=None,
            search_requested=True,
            fast_search_requested=False,
            metrics=metrics,
        )

        self.assertEqual(text, "Synthesized earnings comparison.")
        self.assertEqual([call["feature"] for call in fake_llm.calls], ["chat_reply", "chat_reply_evidence"])
        self.assertNotIn("chat_reply_final", [call["feature"] for call in fake_llm.calls])
        self.assertNotIn("chat_reply_synthesis", [call["feature"] for call in fake_llm.calls])
        self.assertNotIn("chat_final:", str(metrics.get("agent_trace", "")))


class _FakeLoopLLM:
    def __init__(self, turns: list[object], *, synthesis_text: str) -> None:
        self.turns = list(turns)
        self.synthesis_text = synthesis_text
        self.calls: list[dict[str, object]] = []

    async def complete_chat_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        feature = str(kwargs["feature"])
        if feature == "chat_reply_synthesis":
            return _chat_turn(feature=feature, text=self.synthesis_text, prompt_tokens=400, completion_tokens=60)
        if not self.turns:
            return _chat_turn(feature=feature, text="unexpected extra call")
        return self.turns.pop(0)


def _build_test_orchestrator(orchestrator_cls, fake_llm: _FakeLoopLLM):  # type: ignore[no-untyped-def]
    orchestrator = object.__new__(orchestrator_cls)
    orchestrator.settings = SimpleNamespace(
        max_completion_tokens=700,
        openai_memory_model="synthesis-model",
        tool_answer_rewrite_enabled=True,
        tool_answer_rewrite_min_chars=260,
    )
    orchestrator.llm_client = fake_llm

    async def execute_tool(**_kwargs):  # type: ignore[no-untyped-def]
        return (
            "Tavily web results for: NVIDIA AMD earnings\n\n"
            "1. Result\nhttps://example.com\nRevenue and EPS evidence.",
            {"web_search_ms": 1, "web_search_query_count": 1},
        )

    async def record_usage(**_kwargs):  # type: ignore[no-untyped-def]
        return 0, 0

    orchestrator._execute_chat_tool_call = execute_tool  # type: ignore[method-assign]
    orchestrator._record_usage = record_usage  # type: ignore[method-assign]
    return orchestrator


def _chat_turn(
    *,
    feature: str,
    text: str,
    tool_calls: list[object] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> object:
    return SimpleNamespace(
        text=text,
        raw_text=text,
        usage=SimpleNamespace(
            feature=feature,
            model="test-model",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated_cost_usd=0.0,
        ),
        tool_calls=tool_calls or [],
        reasoning_content="",
        finish_reason="stop",
    )


if __name__ == "__main__":
    unittest.main()
