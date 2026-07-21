from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nycti.chat.action_confirmation import append_authoritative_action_cards
from nycti.chat.evidence import (
    CitationAudit,
    EvidenceItem,
    EvidenceLedger,
    build_evidence_ledger,
)
from nycti.chat.loop_messages import append_assistant_tool_call_message
from nycti.chat.orchestrator_support import CURRENT_PRICE_REQUEST_RE, increment_metric
from nycti.chat.run_state import (
    AgentRun,
    AnswerProfile,
    CorrectionKind,
    EvidenceMode,
    ToolStatus,
)

if TYPE_CHECKING:
    from nycti.llm.client import LLMChatTurn

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", re.IGNORECASE)
_EVIDENCE_LINK_RE = re.compile(
    r"[ \t]*\[E-[A-Z0-9]{1,64}\]\(https?://[^\s)]+\)",
    re.IGNORECASE,
)
_EVIDENCE_MARKER_RE = re.compile(r"[ \t]*\[E-[A-Z0-9]{1,64}\]", re.IGNORECASE)
_SOURCE_SECTION_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?[*_]{0,2}(?:sources?|references?)"
    r"[*_]{0,2}\s*:?\s*[*_]{0,2}(?:\s*(?:[-*]\s*)?"
    r"(?:\[[^\]\n]+\]\(https?://[^\s)]+\)|https?://\S+).*)?$"
)
_PRECISE_EVIDENCE_TOOLS = frozenset(
    {
        "annual_perf",
        "browser_extract",
        "price_hist",
        "calc",
        "quote",
        "url_extract",
        "yt_transcript",
    }
)
_BROAD_QUOTE_COVERAGE_MIN = 8


def append_evidence_guidance(
    run: AgentRun,
    *,
    metrics: dict[str, int | str] | None,
    request_text: str = "",
) -> None:
    ledger = build_evidence_ledger(run.outcomes)
    _record_ledger_metrics(ledger, metrics)
    candidate_items = tuple(
        item
        for item in ledger.items
        if _evidence_guidance_quality(item)
        > run.guided_evidence_quality.get(item.evidence_id, (-10_000, -1))
    )
    if not candidate_items:
        return
    rendered = EvidenceLedger(candidate_items).render_bounded_model_guidance(
        include_citations=run.evidence_mode == EvidenceMode.CITED,
    )
    if not rendered.items:
        return
    for item in rendered.items:
        run.guided_evidence_ids.add(item.evidence_id)
        run.guided_evidence_quality[item.evidence_id] = _evidence_guidance_quality(item)
    tool_names = {item.tool_name for item in rendered.items}
    decision_lines = [
        "Tool-result decision: if the successful results cover the request, answer now. Call another tool only "
        "for a concrete unanswered requirement; do not broadly re-verify a successful specialized result."
    ]
    if "deep_research" in tool_names:
        decision_lines.append(
            "When deep_research supplies the requested facts or comparison for every named subject, answer "
            "immediately from that result. Do not call web or url_extract merely to recheck, broaden, identify, "
            "or independently confirm the same facts; deep_research already performed search, extraction, and "
            "reduction. Call another tool only when a specific user-requested fact is absent or the result "
            "explicitly reports a failure or conflict."
        )
    if "annual_perf" in tool_names:
        decision_lines.append(
            "annual_perf is sufficient for requested calendar-year returns and distributions unless a field is missing."
        )
    if (
        "web" in tool_names
        and CURRENT_PRICE_REQUEST_RE.search(request_text)
        and "quote" not in run.successful_tools
        and run.answer_plan is not None
        and "quote" in run.answer_plan.eligible_tool_names
    ):
        decision_lines.append(
            "This is a current-price request. If the web result names a plausible public ticker, call quote next. "
            "Do not call url_extract, browser_extract, web, or deep_research merely to recheck that ticker or price; "
            "quote is the authoritative next step."
        )
    quote_coverage = max(
        (
            int(outcome.metrics.get("stock_quote_success_symbol_count", 0))
            for outcome in run.outcomes
            if outcome.tool_name == "quote" and outcome.status == ToolStatus.OK
        ),
        default=0,
    )
    valuation_coverage = max(
        (
            int(outcome.metrics.get("stock_quote_valuation_symbol_count", 0))
            for outcome in run.outcomes
            if outcome.tool_name == "quote" and outcome.status == ToolStatus.OK
        ),
        default=0,
    )
    if "quote" in tool_names and valuation_coverage >= 2:
        decision_lines.append(
            "The quote batch returned public-company market caps and share counts for both sides. Use those "
            "same-time numeric inputs for the valuation comparison or price threshold and answer now; do not "
            "replace them with intraday ranking headlines."
        )
    if "quote" in tool_names and quote_coverage >= _BROAD_QUOTE_COVERAGE_MIN:
        decision_lines.append(
            f"A quote batch returned live data for {quote_coverage} instruments. If those instruments cover the requested "
            "sector or universe, synthesize the snapshot now. Do not call calc merely to rank, compare, or format "
            "the returned moves, and request more quotes only for a concrete missing instrument required by the user."
        )
    run.messages.append(
        {"role": "user", "content": rendered.text + "\n\n" + " ".join(decision_lines)}
    )


def _evidence_guidance_quality(item: EvidenceItem) -> tuple[int, int]:
    return (-item.source_count, int(item.tool_name in _PRECISE_EVIDENCE_TOOLS))


def request_evidence_repair(
    run: AgentRun,
    turn: LLMChatTurn,
    *,
    metrics: dict[str, int | str] | None,
) -> bool:
    if run.evidence_mode != EvidenceMode.CITED:
        return False
    ledger = build_evidence_ledger(run.outcomes)
    audit = _audit(run, ledger, turn.text)
    _record_audit_metrics(audit, metrics)
    if (
        audit.valid
        or not ledger.items
        or not run.use_correction(CorrectionKind.EVIDENCE_REPAIR)
    ):
        return False

    append_assistant_tool_call_message(run.messages, turn)
    run.messages.append(
        {
            "role": "user",
            "content": _repair_prompt(ledger, audit),
        }
    )
    increment_metric(metrics, "evidence_repair_count")
    return True


def prepare_answer_for_delivery(
    run: AgentRun,
    answer: str,
    *,
    metrics: dict[str, int | str] | None,
) -> str:
    ledger = build_evidence_ledger(run.outcomes)
    if not ledger.items:
        safe_answer = (
            answer
            if run.evidence_mode == EvidenceMode.CITED
            else _remove_internal_evidence_display(answer)
        )
        return append_authoritative_action_cards(safe_answer, run.outcomes)
    audit = _audit(run, ledger, answer)
    _record_ledger_metrics(ledger, metrics)
    _record_audit_metrics(audit, metrics)
    display_answer = (
        answer
        if run.evidence_mode == EvidenceMode.CITED
        else _remove_internal_evidence_display(answer)
    )
    safe_answer = _remove_untrusted_references(display_answer, audit)
    if safe_answer != answer:
        increment_metric(metrics, "evidence_sanitized_answer_count")

    source_list = (
        _required_source_list(ledger, audit, safe_answer)
        if run.evidence_mode == EvidenceMode.CITED
        else ""
    )
    if source_list:
        safe_answer = f"{safe_answer.rstrip()}\n\n{source_list}"
    return append_authoritative_action_cards(safe_answer, run.outcomes)


def _audit(run: AgentRun, ledger: EvidenceLedger, answer: str) -> CitationAudit:
    require_citations = run.evidence_mode == EvidenceMode.CITED and (
        ledger.researched
        or bool(
            ledger.items
            and run.answer_plan is not None
            and run.answer_plan.profile == AnswerProfile.DEEP
        )
    )
    return ledger.audit_answer(answer, researched=require_citations)


def _repair_prompt(ledger: EvidenceLedger, audit: CitationAudit) -> str:
    problems: list[str] = []
    if audit.unknown_citation_ids:
        problems.append("unknown evidence IDs: " + ", ".join(audit.unknown_citation_ids))
    if audit.unprovenanced_urls:
        problems.append("URLs absent from tool provenance: " + ", ".join(audit.unprovenanced_urls))
    if audit.lacks_citations:
        problems.append("researched claims have no evidence citation")
    return (
        "Revise the answer once to satisfy the evidence contract ("
        + "; ".join(problems)
        + "). Cite supporting IDs exactly as `[E-…]`, use no URL outside the ledger, "
        "and state uncertainty where the evidence is insufficient. Do not call more tools.\n\n"
        + ledger.render_model_guidance()
    )


def _required_source_list(
    ledger: EvidenceLedger,
    audit: CitationAudit,
    answer: str,
) -> str:
    selected_ids = audit.cited_ids or None
    selected_items = [
        item
        for item in ledger.items
        if selected_ids is None or item.evidence_id in selected_ids
    ]
    if selected_items and all(
        item.source in answer for item in selected_items if item.is_external
    ) and any(item.is_external for item in selected_items):
        return ""
    include_tool_evidence = audit.researched and not ledger.researched
    return ledger.render_source_list(
        selected_ids,
        include_tool_evidence=include_tool_evidence,
    )


def _remove_untrusted_references(answer: str, audit: CitationAudit) -> str:
    bad_urls = set(audit.unprovenanced_urls)

    def replace_markdown_link(match: re.Match[str]) -> str:
        if match.group(2) not in bad_urls:
            return match.group(0)
        return f"{match.group(1)} (unverified link omitted)"

    cleaned = _MARKDOWN_LINK_RE.sub(replace_markdown_link, answer)
    for url in sorted(bad_urls, key=len, reverse=True):
        cleaned = cleaned.replace(url, "[unverified link omitted]")
    for evidence_id in audit.unknown_citation_ids:
        cleaned = re.sub(
            rf"\[{re.escape(evidence_id)}\]",
            "[unsupported citation omitted]",
            cleaned,
            flags=re.IGNORECASE,
        )
    return cleaned


def _remove_evidence_markers(answer: str) -> str:
    cleaned = _EVIDENCE_LINK_RE.sub("", answer)
    cleaned = _EVIDENCE_MARKER_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return re.sub(r" {2,}", " ", cleaned)


def _remove_internal_evidence_display(answer: str) -> str:
    source_section = _SOURCE_SECTION_RE.search(answer)
    without_sources = answer[: source_section.start()].rstrip() if source_section else answer
    return _remove_evidence_markers(without_sources)


def _record_ledger_metrics(
    ledger: EvidenceLedger,
    metrics: dict[str, int | str] | None,
) -> None:
    if metrics is None:
        return
    metrics["evidence_item_count"] = len(ledger.items)
    metrics["evidence_external_source_count"] = len(ledger.provenance_urls)


def _record_audit_metrics(
    audit: CitationAudit,
    metrics: dict[str, int | str] | None,
) -> None:
    if metrics is None or audit.valid:
        return
    increment_metric(metrics, "evidence_audit_failure_count")
    metrics["evidence_unknown_citation_count"] = len(audit.unknown_citation_ids)
    metrics["evidence_unprovenanced_url_count"] = len(audit.unprovenanced_urls)
    metrics["evidence_missing_citation_count"] = int(audit.lacks_citations)
