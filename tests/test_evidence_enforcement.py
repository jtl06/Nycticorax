from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.chat.evidence import build_evidence_ledger
from nycti.chat.evidence_enforcement import (
    append_evidence_guidance,
    prepare_answer_for_delivery,
    request_evidence_repair,
)
from nycti.chat.run_state import (
    AgentBudget,
    AgentRun,
    AnswerPlan,
    AnswerProfile,
    CorrectionKind,
    EvidenceMode,
    ToolOutcome,
    ToolStatus,
)
from nycti.chat.tool_fallback import fallback_tool_result


class EvidenceEnforcementTests(unittest.TestCase):
    def test_guidance_exposes_stable_ids_and_only_observed_urls(self) -> None:
        run = _run()

        append_evidence_guidance(run, metrics=None)
        message_count = len(run.messages)
        append_evidence_guidance(run, metrics=None)

        evidence_id = build_evidence_ledger(run.outcomes).items[0].evidence_id
        guidance = str(run.messages[-1]["content"])
        self.assertIn(evidence_id, guidance)
        self.assertIn("https://example.com/report", guidance)
        self.assertIn("never invent or alter", guidance)
        self.assertIn("if the successful results cover the request, answer now", guidance)
        self.assertEqual(message_count, len(run.messages))

    def test_guidance_marks_composite_and_annual_results_as_self_contained(self) -> None:
        run = _run()
        run.outcomes = [
            ToolOutcome(
                call_id="deep",
                tool_name="deep_research",
                arguments='{"question":"compare"}',
                status=ToolStatus.OK,
                content="Deep evidence.",
            ),
            ToolOutcome(
                call_id="annual",
                tool_name="annual_perf",
                arguments='{"symbols":["ALFA"]}',
                status=ToolStatus.OK,
                content="Annual evidence.",
            ),
        ]

        append_evidence_guidance(run, metrics=None)

        guidance = str(run.messages[-1]["content"])
        self.assertIn("deep_research already performed", guidance)
        self.assertIn("annual_perf is sufficient", guidance)

    def test_invalid_candidate_gets_one_bounded_repair(self) -> None:
        run = _run()
        metrics: dict[str, int | str] = {}
        turn = _turn("Claim with https://invented.example/report")

        requested = request_evidence_repair(run, turn, metrics=metrics)

        self.assertTrue(requested)
        self.assertEqual(1, run.corrections)
        self.assertEqual({CorrectionKind.EVIDENCE_REPAIR}, run.correction_kinds)
        self.assertEqual(1, metrics["evidence_repair_count"])
        self.assertIn("URLs absent from tool provenance", str(run.messages[-1]["content"]))
        self.assertFalse(request_evidence_repair(run, turn, metrics=metrics))
        self.assertEqual(1, run.corrections)

    def test_delivery_removes_untrusted_references_and_appends_canonical_sources(self) -> None:
        run = _run()
        metrics: dict[str, int | str] = {}
        evidence_id = build_evidence_ledger(run.outcomes).items[0].evidence_id

        answer = prepare_answer_for_delivery(
            run,
            (
                f"Supported claim [{evidence_id}]. See "
                "[made-up source](https://invented.example/report)."
            ),
            metrics=metrics,
        )

        self.assertNotIn("https://invented.example/report", answer)
        self.assertIn("made-up source (unverified link omitted)", answer)
        self.assertIn("Sources:", answer)
        self.assertIn("https://example.com/report", answer)
        self.assertEqual(1, metrics["evidence_sanitized_answer_count"])

    def test_known_source_url_is_not_duplicated(self) -> None:
        run = _run()

        answer = prepare_answer_for_delivery(
            run,
            "Supported by https://example.com/report.",
            metrics=None,
        )

        self.assertEqual(1, answer.count("https://example.com/report"))

    def test_deep_tool_only_answer_requires_and_lists_tool_evidence(self) -> None:
        run = _run(profile=AnswerProfile.DEEP, external=False)
        turn = _turn("The calculation is 42.")

        self.assertTrue(request_evidence_repair(run, turn, metrics=None))
        evidence_id = build_evidence_ledger(run.outcomes).items[0].evidence_id
        delivered = prepare_answer_for_delivery(
            run,
            f"The calculation is 42. [{evidence_id}]",
            metrics=None,
        )

        self.assertIn("`python` tool result", delivered)

    def test_internal_mode_grounds_without_repairing_or_displaying_sources(self) -> None:
        source_sections = (
            "Sources:\n- [report](https://example.com/report)",
            "### Sources:\n- [report](https://example.com/report)",
            "**Sources:**\n- [report](https://example.com/report)",
            "References\n- [report](https://example.com/report)",
            "_Source:_\n- [report](https://example.com/report)",
            "Sources: [report](https://example.com/report)",
            "**Sources:** [report](https://example.com/report)",
            "References: https://example.com/report",
        )
        for source_section in source_sections:
            with self.subTest(source_section=source_section):
                run = _run(evidence_mode=EvidenceMode.INTERNAL)
                append_evidence_guidance(run, metrics=None)
                evidence_id = build_evidence_ledger(run.outcomes).items[0].evidence_id

                self.assertIn("internal grounding only", str(run.messages[-1]["content"]))
                self.assertFalse(request_evidence_repair(run, _turn("Supported claim."), metrics=None))
                delivered = prepare_answer_for_delivery(
                    run,
                    f"Supported claim [{evidence_id}](https://example.com/report).\n\n{source_section}",
                    metrics=None,
                )

                self.assertEqual("Supported claim.", delivered)
                self.assertNotIn(evidence_id, delivered)

    def test_generic_fallback_hides_sources_unless_cited_mode_requests_them(self) -> None:
        raw = "Deep evidence. URL: https://example.com/report\n- [Report](https://example.com/report)"

        internal = fallback_tool_result(raw)
        self.assertNotIn("https://example.com", internal)
        self.assertNotIn("[]()", internal)
        self.assertNotIn("URL:", internal)
        self.assertIn("Report", internal)
        self.assertIn("https://example.com", fallback_tool_result(raw, include_sources=True))

    def test_internal_mode_preserves_a_provenanced_inline_link(self) -> None:
        run = _run(evidence_mode=EvidenceMode.INTERNAL)

        delivered = prepare_answer_for_delivery(
            run,
            "Requested link: https://example.com/report",
            metrics=None,
        )

        self.assertIn("https://example.com/report", delivered)


def _run(
    *,
    profile: AnswerProfile = AnswerProfile.GROUNDED,
    external: bool = True,
    evidence_mode: EvidenceMode = EvidenceMode.CITED,
) -> AgentRun:
    source = ("https://example.com/report",) if external else ()
    run = AgentRun(
        messages=[{"role": "user", "content": "Research this"}],
        budget=AgentBudget(max_corrections=1),
        answer_plan=AnswerPlan(
            profile=profile,
            eligible_tool_names=frozenset(),
            budget=AgentBudget(),
        ),
        evidence_mode=evidence_mode,
    )
    run.outcomes.append(
        ToolOutcome(
            call_id="call-1",
            tool_name="web" if external else "python",
            arguments='{"query":"example"}',
            status=ToolStatus.OK,
            content="The official report supports the claim.",
            provenance=source,
        )
    )
    return run


def _turn(text: str) -> object:
    return SimpleNamespace(
        text=text,
        tool_calls=[],
        response_output_items=[],
    )


if __name__ == "__main__":
    unittest.main()
