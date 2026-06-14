from __future__ import annotations

from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.orchestrator_support import (
    first_result_line,
    looks_like_raw_tavily_dump,
    write_agent_trace,
)
from nycti.chat.run_state import AgentRun, AgentStep, StopReason, ToolOutcome, ToolStatus
from nycti.chat.tool_fallback import fallback_tool_result

if TYPE_CHECKING:
    from nycti.llm.client import LLMChatTurn


def append_assistant_tool_call_message(
    messages: list[dict[str, object]],
    turn: LLMChatTurn,
) -> None:
    messages.append(
        {
            "role": "assistant",
            "content": turn.text,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in turn.tool_calls
            ],
        }
    )


def append_skipped_tool_result(
    run: AgentRun,
    tool_call: object,
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
        return fallback_tool_result(raw_text)
    for outcome in reversed(run.outcomes):
        if outcome.status == ToolStatus.OK and outcome.content:
            return fallback_tool_result(outcome.content)
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
        metrics["agent_correction_count"] = run.corrections
        metrics["agent_continuation_count"] = run.continuations
        metrics["agent_stop_reason"] = str(run.stop_reason or StopReason.FINAL_TEXT)
    write_agent_trace(metrics, trace)
    return text, reasoning
