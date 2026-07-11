from __future__ import annotations

from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.orchestrator_support import (
    first_result_line,
    looks_like_raw_tavily_dump,
    write_agent_trace,
)
from nycti.chat.run_state import AgentRun, AgentStep, EvidenceMode, StopReason, ToolOutcome, ToolStatus
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.llm.responses_adapter import RESPONSES_OUTPUT_ITEMS_KEY

if TYPE_CHECKING:
    from nycti.llm.client import LLMChatTurn
    from nycti.llm.tool_calls import LLMToolCall


def append_assistant_tool_call_message(
    messages: list[dict[str, object]],
    turn: LLMChatTurn,
) -> None:
    messages.append(build_assistant_turn_message(turn))


def build_assistant_turn_message(turn: LLMChatTurn) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "content": turn.text,
    }
    tool_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in turn.tool_calls
    ]
    if tool_calls:
        message["tool_calls"] = tool_calls
    response_output_items = getattr(turn, "response_output_items", [])
    if response_output_items:
        message[RESPONSES_OUTPUT_ITEMS_KEY] = response_output_items
    return message


def append_skipped_tool_result(
    run: AgentRun,
    tool_call: LLMToolCall,
    *,
    reason: str,
) -> None:
    run.messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": reason,
        }
    )


def append_tool_outcomes(
    run: AgentRun,
    outcomes: list[ToolOutcome],
    *,
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
) -> None:
    for outcome in outcomes:
        run.outcomes.append(outcome)
        run.usage_records.extend(outcome.usage_records)
        run.messages.append(
            {
                "role": "tool",
                "tool_call_id": outcome.call_id,
                "name": outcome.tool_name,
                "content": outcome.model_content(),
            }
        )
        trace.add(
            f"tool:{outcome.tool_name}",
            elapsed_ms=outcome.latency_ms,
            attrs={"status": outcome.status, "result": first_result_line(outcome.content)},
        )
        if metrics is not None:
            metrics["tool_call_count"] = int(metrics.get("tool_call_count", 0)) + 1
            for key, value in outcome.metrics.items():
                if isinstance(value, int):
                    metrics[key] = int(metrics.get(key, 0)) + value
                else:
                    metrics[key] = value


def fallback_text(run: AgentRun, raw_text: str = "") -> str:
    if raw_text and looks_like_raw_tavily_dump(raw_text):
        return fallback_tool_result(
            raw_text,
            include_sources=run.evidence_mode == EvidenceMode.CITED,
        )
    for outcome in reversed(run.outcomes):
        if outcome.status == ToolStatus.OK and outcome.content:
            return fallback_tool_result(
                outcome.content,
                include_sources=run.evidence_mode == EvidenceMode.CITED,
            )
    return "I couldn't generate a clean reply from that request. Try rephrasing it a bit."


def finish_run(
    run: AgentRun,
    text: str,
    reasoning: list[str],
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
) -> tuple[str, list[str]]:
    run.step = AgentStep.DONE
    if metrics is not None:
        metrics["agent_model_turn_count"] = run.model_turns
        metrics["agent_tool_call_count"] = run.tool_calls
        metrics["agent_tool_cost_units"] = run.tool_cost_units
        metrics["agent_deep_research_call_count"] = run.deep_research_calls
        metrics["agent_correction_count"] = run.corrections
        metrics["agent_correction_categories"] = (
            ", ".join(sorted(str(kind) for kind in run.correction_kinds)) or "(none)"
        )
        metrics["agent_continuation_count"] = run.continuations
        metrics["agent_total_tokens"] = sum(
            max(int(getattr(usage, "total_tokens", 0)), 0)
            for usage in run.usage_records
        )
        metrics["agent_stop_reason"] = str(run.stop_reason or StopReason.FINAL_TEXT)
        metrics["agent_final_status"] = run.final_status
        if run.final_failure_reason:
            metrics["agent_final_failure_reason"] = run.final_failure_reason
    write_agent_trace(metrics, trace)
    return text, reasoning
