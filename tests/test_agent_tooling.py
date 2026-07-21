import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.orchestrator_support import (
    format_available_tool_guidance,
    quote_verification_prompt_for_price_answer,
)
from nycti.chat.tool_eligibility import (
    READ_ONLY_TOOL_NAMES,
    select_answer_plan,
    select_eligible_tools,
)
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.registry import TOOL_SPECS
from nycti.chat.tools.schemas import build_chat_tools

GUILD_TOOL_NAMES = {"reminder", "report_issue", "send_msg"}


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

    def test_native_tool_names_avoid_provider_reserved_python_name(self) -> None:
        names = {
            tool["function"]["name"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        }

        self.assertIn("calc", names)
        self.assertNotIn("python", names)

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
                "calc",
                "url_extract",
                "web",
            },
            "summarize what happened in the channel earlier today": {"channel_ctx"},
            "chip companies > $100b today": {"quote", "url_extract", "web"},
            "why are memory stocks down today?": {"quote", "url_extract", "web"},
            "what do you remember about my database preferences?": set(),
        }

        for prompt, expected in prompts.items():
            with self.subTest(prompt=prompt):
                eligible, _ = select_eligible_tools(
                    request_text=prompt,
                    guild_id=1,
                )
                plan, _ = select_answer_plan(request_text=prompt, guild_id=1)
                self.assertEqual(
                    set(READ_ONLY_TOOL_NAMES) | GUILD_TOOL_NAMES,
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

        self.assertTrue(GUILD_TOOL_NAMES.issubset(ordinary))
        self.assertTrue(GUILD_TOOL_NAMES.isdisjoint(direct_message))

    def test_tool_guidance_allows_natural_response_feedback(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"report_issue"})

        self.assertIn("call report_issue once", guidance)
        self.assertIn("Do not wait for the exact phrase 'bad bot'", guidance)

    def test_tool_guidance_covers_volatile_company_status(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"web", "quote"})

        self.assertIn("For live/current asks", guidance)
        self.assertIn("how did X do today", guidance)
        self.assertIn("volatile company-status facts", guidance)
        self.assertIn("IPO/public status", guidance)
        self.assertIn("current evidence", guidance)
        self.assertIn("model memory", guidance)
        self.assertIn("For current price asks with a ticker-form symbol", guidance)
        self.assertIn("what's USD/JPY?", guidance)
        self.assertIn("Pass FX pairs as BASE/QUOTE", guidance)
        self.assertIn("Batch all known requested symbols", guidance)
        self.assertIn("retry only the failed symbols once", guidance)
        self.assertIn("market-cap comparisons", guidance)
        self.assertIn("shares-outstanding fields", guidance)
        self.assertIn("establish breadth and cause", guidance)
        self.assertIn("Request both in the same turn when possible", guidance)
        self.assertIn("Do not generalize one company", guidance)
        self.assertIn("combined public/private valuations", guidance)
        self.assertIn("ignore token pages", guidance)
        self.assertIn("requested local or non-English research", guidance)
        self.assertIn("set country to the English country name", guidance)
        self.assertLess(len(guidance), 2350)

    def test_tool_guidance_fetches_missing_social_context(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"channel_ctx", "web"}
        )

        self.assertIn("why another member said something", guidance)
        self.assertIn("what changed since an earlier exchange", guidance)
        self.assertIn("use channel_ctx before inferring", guidance)
        self.assertIn("short callback whose referent does not clearly fit", guidance)
        self.assertIn("ask one narrow clarification", guidance)
        self.assertIn("treat human messages as the source", guidance)
        self.assertIn("prior Nycti paraphrase is not proof", guidance)

    def test_tool_guidance_only_includes_relevant_sections(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"calc"})

        self.assertNotIn("current price", guidance)
        self.assertNotIn("investor-relations", guidance)
        self.assertLess(len(guidance), 500)

    def test_promotion_guidance_prefers_smallest_sufficient_tool_set(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"deep_research", "web"},
            promoted_tool_names=("web",),
        )

        self.assertIn("Other available tools remain callable", guidance)
        self.assertIn("smallest promoted tool or combination", guidance)

    def test_quote_recovery_covers_terse_stock_now_without_affecting_earnings(self) -> None:
        prompt = quote_verification_prompt_for_price_answer(
            request_text="ACME stock now?",
            answer_text='I cannot verify a current listing for "ACME".',
            available_tool_names={"quote", "web"},
            used_tool_names={"web"},
        )

        self.assertIsNotNone(prompt)
        self.assertIn("ACME", str(prompt))

        earnings = quote_verification_prompt_for_price_answer(
            request_text="Compare NVIDIA and AMD latest earnings and guidance.",
            answer_text="NVDA and AMD both reported results.",
            available_tool_names={"quote", "web"},
            used_tool_names={"web"},
        )
        self.assertIsNone(earnings)


if __name__ == "__main__":
    unittest.main()
