from __future__ import annotations

import unittest

from nycti.chat.evidence import EvidenceLedger, build_evidence_ledger
from nycti.chat.run_state import ToolOutcome, ToolStatus
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
)


class EvidenceLedgerTests(unittest.TestCase):
    def test_builds_bounded_ledger_from_successful_outcomes(self) -> None:
        outcomes = [
            _outcome(
                call_id="one",
                tool_name="web",
                content=(
                    "1. Official results\nhttps://example.com/results\n"
                    "Revenue increased by 20 percent."
                ),
                provenance=("https://example.com/results",),
            ),
            _outcome(
                call_id="bad",
                tool_name="web",
                status=ToolStatus.ERROR,
                content="Failed: https://bad.example/failure",
                provenance=("https://bad.example/failure",),
            ),
            _outcome(
                call_id="two",
                tool_name="url_extract",
                content="Second source content.",
                provenance=("https://example.org/report",),
            ),
        ]

        ledger = build_evidence_ledger(outcomes, max_items=1)

        self.assertEqual(1, len(ledger.items))
        item = ledger.items[0]
        self.assertRegex(item.evidence_id, r"^E-[A-F0-9]{10}$")
        self.assertEqual("web", item.tool_name)
        self.assertEqual("https://example.com/results", item.source)
        self.assertIn("Revenue increased", item.excerpt)

    def test_ids_are_stable_and_duplicate_sources_are_removed(self) -> None:
        first = _outcome(
            call_id="one",
            tool_name="web",
            content="Result A",
            provenance=("HTTPS://Example.com/report#section",),
        )
        duplicate = _outcome(
            call_id="two",
            tool_name="url_extract",
            content="Result B",
            provenance=("https://example.com/report",),
        )

        ledger = EvidenceLedger.from_outcomes([first, duplicate])
        rebuilt = EvidenceLedger.from_outcomes([first])

        self.assertEqual(1, len(ledger.items))
        self.assertEqual(rebuilt.items[0].evidence_id, ledger.items[0].evidence_id)
        self.assertEqual("url_extract", ledger.items[0].tool_name)
        self.assertEqual("https://example.com/report", ledger.items[0].source)

    def test_precise_later_extracts_are_not_crowded_out_by_broad_results(self) -> None:
        broad_urls = tuple(f"https://broad.example/{index}" for index in range(20))
        outcomes = [
            _outcome(
                call_id="deep",
                tool_name="deep_research",
                content="\n".join(broad_urls),
                provenance=broad_urls,
            ),
            _outcome(
                call_id="exact-one",
                tool_name="url_extract",
                content="Official release with complete evidence.",
                provenance=("https://official.example/release",),
            ),
            _outcome(
                call_id="exact-two",
                tool_name="url_extract",
                content="Official documentation with complete evidence.",
                provenance=("https://official.example/docs",),
            ),
        ]

        ledger = EvidenceLedger.from_outcomes(outcomes)

        self.assertEqual(22, len(ledger.items))
        self.assertTrue(set(broad_urls).issubset(ledger.provenance_urls))
        self.assertIn("https://official.example/release", ledger.provenance_urls)
        self.assertIn("https://official.example/docs", ledger.provenance_urls)
        self.assertEqual("url_extract", ledger.items[0].tool_name)
        self.assertEqual("url_extract", ledger.items[1].tool_name)

    def test_success_without_url_becomes_bounded_tool_evidence(self) -> None:
        outcome = _outcome(
            call_id="quote-1",
            tool_name="quote",
            arguments='{"symbols":["NVDA"]}',
            content="NVDA last price: 190.25",
        )

        ledger = EvidenceLedger.from_outcomes([outcome], max_excerpt_chars=40)

        self.assertEqual(1, len(ledger.items))
        self.assertTrue(ledger.items[0].source.startswith("tool:quote:"))
        self.assertFalse(ledger.items[0].is_external)
        self.assertFalse(ledger.researched)
        self.assertIn("quote", ledger.render_source_list(include_tool_evidence=True))

    def test_tool_evidence_id_is_stable_across_json_key_order(self) -> None:
        first = _outcome(
            call_id="one",
            tool_name="quote",
            arguments='{"symbols":["NVDA"],"mode":"live"}',
            content="NVDA: 190.25",
        )
        second = _outcome(
            call_id="two",
            tool_name="quote",
            arguments='{"mode":"live", "symbols":["NVDA"]}',
            content="NVDA: 190.25",
        )

        first_id = EvidenceLedger.from_outcomes([first]).items[0].evidence_id
        second_id = EvidenceLedger.from_outcomes([second]).items[0].evidence_id

        self.assertEqual(first_id, second_id)

    def test_renders_concise_model_guidance_and_source_list(self) -> None:
        ledger = EvidenceLedger.from_outcomes([
            _outcome(
                call_id="one",
                tool_name="web",
                content="Primary filing says revenue was $10 billion.",
                provenance=("https://investor.example.com/filing",),
            )
        ])
        item = ledger.items[0]

        guidance = ledger.render_model_guidance()
        sources = ledger.render_source_list([item.evidence_id])

        self.assertIn(f"[{item.evidence_id}]", guidance)
        self.assertIn("never invent or alter", guidance)
        self.assertIn("Primary filing", guidance)
        self.assertIn(f"[{item.evidence_id}]", sources)
        self.assertIn("https://investor.example.com/filing", sources)

    def test_audit_accepts_known_evidence_citation(self) -> None:
        ledger = _researched_ledger()
        evidence_id = ledger.items[0].evidence_id

        audit = ledger.audit_answer(f"Revenue increased by 20%. [{evidence_id}]")

        self.assertEqual((evidence_id,), audit.cited_ids)
        self.assertFalse(audit.lacks_citations)
        self.assertEqual((), audit.unprovenanced_urls)
        self.assertTrue(audit.valid)

    def test_audit_detects_urls_missing_from_provenance(self) -> None:
        ledger = _researched_ledger()
        evidence_id = ledger.items[0].evidence_id

        audit = ledger.audit_answer(
            f"See [{evidence_id}] and https://invented.example/report."
        )

        self.assertEqual(("https://invented.example/report",), audit.unprovenanced_urls)
        self.assertFalse(audit.valid)

    def test_audit_detects_researched_answer_without_citation(self) -> None:
        ledger = _researched_ledger()

        audit = ledger.audit_answer("Revenue increased by 20 percent.")

        self.assertTrue(audit.researched)
        self.assertTrue(audit.lacks_citations)
        self.assertFalse(audit.valid)

    def test_audit_accepts_canonical_equivalent_provenance_url(self) -> None:
        ledger = _researched_ledger()

        audit = ledger.audit_answer("Source: https://EXAMPLE.com/results/#details")

        self.assertFalse(audit.lacks_citations)
        self.assertEqual((), audit.unprovenanced_urls)
        self.assertTrue(audit.valid)

    def test_audit_flags_unknown_evidence_id(self) -> None:
        ledger = _researched_ledger()

        audit = ledger.audit_answer("Unsupported claim [E-0000000000].")

        self.assertEqual(("E-0000000000",), audit.unknown_citation_ids)
        self.assertTrue(audit.lacks_citations)

        malformed = ledger.audit_answer("Unsupported claim [E-NOTREAL].")
        self.assertEqual(("E-NOTREAL",), malformed.unknown_citation_ids)

    def test_caller_can_require_citations_without_external_urls(self) -> None:
        ledger = EvidenceLedger.from_outcomes([
            _outcome(call_id="one", tool_name="quote", content="NVDA: 190.25")
        ])

        audit = ledger.audit_answer("NVDA is 190.25.", researched=True)

        self.assertTrue(audit.researched)
        self.assertTrue(audit.lacks_citations)

    def test_action_proposals_are_not_answer_evidence(self) -> None:
        outcomes = [
            _outcome(
                call_id="send",
                tool_name=SEND_CHANNEL_MESSAGE_TOOL_NAME,
                content="Proposed action: send a message after confirmation.",
            ),
            _outcome(
                call_id="reminder",
                tool_name=CREATE_REMINDER_TOOL_NAME,
                content="Proposed action: create a reminder after confirmation.",
            ),
        ]

        ledger = EvidenceLedger.from_outcomes(outcomes)

        self.assertEqual((), ledger.items)


def _researched_ledger() -> EvidenceLedger:
    return EvidenceLedger.from_outcomes([
        _outcome(
            call_id="one",
            tool_name="web",
            content="Official results reported 20 percent revenue growth.",
            provenance=("https://example.com/results",),
        )
    ])


def _outcome(
    *,
    call_id: str,
    tool_name: str,
    content: str,
    arguments: str = "{}",
    status: ToolStatus = ToolStatus.OK,
    provenance: tuple[str, ...] = (),
) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        status=status,
        content=content,
        provenance=provenance,
    )
