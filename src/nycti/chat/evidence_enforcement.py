from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nycti.chat.action_confirmation import append_authoritative_action_cards
from nycti.chat.evidence import CitationAudit, EvidenceLedger, build_evidence_ledger
from nycti.chat.loop_messages import append_assistant_tool_call_message
from nycti.chat.orchestrator_support import increment_metric
from nycti.chat.run_state import AgentRun, AnswerProfile, CorrectionKind

if TYPE_CHECKING:
    from nycti.llm.client import LLMChatTurn

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", re.IGNORECASE)


def append_evidence_guidance(
    run: AgentRun,
    *,
    metrics: dict[str, int | str] | None,
) -> None:
    ledger = build_evidence_ledger(run.outcomes)
    _record_ledger_metrics(ledger, metrics)
    new_items = tuple(
        item for item in ledger.items if item.evidence_id not in run.guided_evidence_ids
    )
    if not new_items:
        return
    run.guided_evidence_ids.update(item.evidence_id for item in new_items)
    run.messages.append(
        {"role": "user", "content": EvidenceLedger(new_items).render_model_guidance()}
    )


def request_evidence_repair(
    run: AgentRun,
    turn: LLMChatTurn,
    *,
    metrics: dict[str, int | str] | None,
) -> bool:
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
        return append_authoritative_action_cards(answer, run.outcomes)
    audit = _audit(run, ledger, answer)
    _record_ledger_metrics(ledger, metrics)
    _record_audit_metrics(audit, metrics)
    safe_answer = _remove_untrusted_references(answer, audit)
    if safe_answer != answer:
        increment_metric(metrics, "evidence_sanitized_answer_count")

    source_list = _required_source_list(ledger, audit, safe_answer)
    if source_list:
        safe_answer = f"{safe_answer.rstrip()}\n\n{source_list}"
    return append_authoritative_action_cards(safe_answer, run.outcomes)


def _audit(run: AgentRun, ledger: EvidenceLedger, answer: str) -> CitationAudit:
    require_citations = ledger.researched or bool(
        ledger.items
        and run.answer_plan is not None
        and run.answer_plan.profile == AnswerProfile.DEEP
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
