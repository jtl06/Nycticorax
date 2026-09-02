import unittest

from nycti.agent_trace import AgentTrace
from nycti.chat.orchestrator_support import (
    extract_ticker_candidates,
    format_available_tool_guidance,
    quote_verification_prompt_for_price_answer,
)
from nycti.chat.run_state import AnswerProfile
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
        tools = {
            tool["function"]["name"]: tool["function"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        }

        self.assertIn("calc", tools)
        self.assertNotIn("python", tools)
        self.assertIn("numpy", tools["calc"]["description"])
        self.assertIn("networkx", tools["calc"]["description"])
        self.assertIn("unsafe", tools["calc"]["description"])

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

    def test_ambiguous_callback_inherits_grounding_hints_from_recent_context(self) -> None:
        plan, _ = select_answer_plan(
            request_text="finish",
            context_text="GTS81: stocks\nNycti: Which stocks should I check?",
            guild_id=1,
        )

        self.assertEqual({"quote", "url_extract", "web"}, set(plan.promoted_tool_names))

    def test_quick_social_reply_does_not_inherit_old_grounding_hints(self) -> None:
        plan, _ = select_answer_plan(
            request_text="thanks",
            context_text="Nycti: NVDA stock is up today.",
            guild_id=1,
        )

        self.assertEqual((), plan.promoted_tool_names)

    def test_empty_recent_context_sentinel_does_not_promote_web(self) -> None:
        plan, _ = select_answer_plan(
            request_text="Give him one playful line.",
            context_text="(no recent context)",
            guild_id=1,
        )

        self.assertEqual((), plan.promoted_tool_names)

    def test_tool_guidance_allows_natural_response_feedback(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"report_issue"})

        self.assertIn("call report_issue once", guidance)
        self.assertIn("Do not wait for the exact phrase 'bad bot'", guidance)
        self.assertIn("Only the current request can trigger response feedback", guidance)
        self.assertIn("generic continuation such as 'finish'", guidance)

    def test_tool_guidance_covers_volatile_company_status(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"web", "quote"},
            promoted_tool_names=("web", "quote"),
        )

        self.assertIn("For live/current asks", guidance)
        self.assertIn("how did X do today", guidance)
        self.assertIn("explicit ticker even without '$'", guidance)
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
        self.assertIn("multiple disjoint quote calls in that same turn", guidance)
        self.assertIn("never invent a symbol by uppercasing the company name", guidance)
        self.assertIn("topic=finance", guidance)
        self.assertIn("Do not generalize one company", guidance)
        self.assertIn("one batched web request in the same turn", guidance)
        self.assertIn("one same-session market or sector query", guidance)
        self.assertIn("do not search again merely to force a cause", guidance)
        self.assertIn("combined public/private valuations", guidance)
        self.assertIn("ignore token pages", guidance)
        self.assertIn("Discord member and speaker names are people", guidance)
        self.assertIn("do not infer what transferred", guidance)
        self.assertIn("requested local or non-English research", guidance)
        self.assertIn("set country to the English country name", guidance)
        self.assertLess(len(guidance), 3300)

    def test_tool_guidance_treats_active_watchlists_as_complete_market_state(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"quote", "web"},
            market_watchlist_symbols=("NVDA", "AMD", "MU", "SNDK"),
            required_quote_symbols=("NVDA", "AMD", "MU", "SNDK"),
        )

        self.assertIn("active market watchlist is: NVDA, AMD, MU, SNDK", guidance)
        self.assertIn("obvious spelling variant", guidance)
        self.assertIn("complete active watchlist", guidance)
        self.assertIn("Do not silently drop one", guidance)

    def test_tool_guidance_fetches_missing_social_context(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"channel_ctx", "web"},
            promoted_tool_names=("channel_ctx",),
        )

        self.assertIn("why another member said something", guidance)
        self.assertIn("what changed since an earlier exchange", guidance)
        self.assertIn("use channel_ctx before inferring", guidance)
        self.assertIn("short callback whose referent does not clearly fit", guidance)
        self.assertIn("ask one narrow clarification", guidance)
        self.assertIn("treat human messages as the source", guidance)
        self.assertIn("prior Nycti paraphrase is not proof", guidance)
        self.assertIn("Never call channel_ctx more than once", guidance)

    def test_tool_guidance_only_includes_relevant_sections(self) -> None:
        guidance = format_available_tool_guidance(available_tool_names={"calc"})

        self.assertNotIn("current price", guidance)
        self.assertNotIn("investor-relations", guidance)
        self.assertLess(len(guidance), 500)

    def test_all_safe_tools_without_promotions_keep_guidance_compact(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names=set(READ_ONLY_TOOL_NAMES),
        )

        self.assertIn("Available tools this turn", guidance)
        self.assertNotIn("For current price asks", guidance)
        self.assertNotIn("use channel_ctx before inferring", guidance)
        self.assertIn("Use memory_search only for missing", guidance)
        self.assertIn("Use channel_ctx only for needed older chat", guidance)
        self.assertIn("speaker names are people, not tickers", guidance)
        self.assertLess(len(guidance), 800)

    def test_promotion_guidance_prefers_smallest_sufficient_tool_set(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"deep_research", "web"},
            promoted_tool_names=("web",),
        )

        self.assertIn("Other available tools remain callable", guidance)
        self.assertIn("smallest promoted tool or combination", guidance)

    def test_deep_guidance_starts_with_composite_research(self) -> None:
        guidance = format_available_tool_guidance(
            available_tool_names={"deep_research", "web"},
            answer_profile=AnswerProfile.DEEP,
            promoted_tool_names=("deep_research",),
        )

        self.assertIn("start multi-source work with one well-scoped deep_research call", guidance)
        self.assertIn("Use direct tools afterward only", guidance)

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

    def test_explicit_ticker_stock_now_omits_irrelevant_url_promotion(self) -> None:
        plan, _ = select_answer_plan(
            request_text="ACME stock now?",
            guild_id=1,
        )

        self.assertEqual(("quote", "web"), plan.promoted_tool_names)

    def test_quote_recovery_resolves_company_name_before_guessing_ticker(self) -> None:
        prompt = quote_verification_prompt_for_price_answer(
            request_text="What's the current price of Example Rocket Company?",
            answer_text="I found conflicting reports about whether it has a public listing.",
            available_tool_names={"quote", "web"},
            used_tool_names={"web"},
        )

        self.assertIsNotNone(prompt)
        self.assertIn("exact public ticker is still unverified", str(prompt))
        self.assertIn("Do not uppercase the company name", str(prompt))

    def test_quote_recovery_treats_terse_market_callback_as_current_quote(self) -> None:
        prompt = quote_verification_prompt_for_price_answer(
            request_text="SANDISKK",
            request_context_text="mat: how are storage stocks doing today?",
            answer_text="SNDK. SanDisk supremacy.",
            available_tool_names={"quote", "web"},
            used_tool_names=set(),
            market_watchlist_symbols=("NVDA", "MU", "SNDK"),
        )

        self.assertIsNotNone(prompt)
        self.assertIn("SNDK", str(prompt))

    def test_ticker_candidates_ignore_evidence_ids_and_uppercase_company_names(self) -> None:
        self.assertEqual(
            ("SPCX",),
            extract_ticker_candidates("[E-17] [E-ABC] [S2] Space company now trades as SPCX."),
        )
        self.assertEqual((), extract_ticker_candidates("The provider returned SPACEX and [E-]."))


if __name__ == "__main__":
    unittest.main()
