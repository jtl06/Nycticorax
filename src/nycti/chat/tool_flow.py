from __future__ import annotations

import asyncio
from collections.abc import Sequence
import hashlib
import time

from nycti.agent_trace import AgentTrace
from nycti.chat.evidence_enforcement import append_evidence_guidance
from nycti.chat.loop_messages import append_skipped_tool_result, append_tool_outcomes
from nycti.chat.orchestrator_support import increment_metric, tool_call_signature
from nycti.chat.run_state import AgentRun, AgentStep, StopReason, ToolOutcome, ToolStatus
from nycti.chat.tools.schemas import GET_CHANNEL_CONTEXT_TOOL_NAME
from nycti.procedures.recipe import argument_field_names
from nycti.progress import ResponseProgressPhase, ResponseProgressReporter, advance_response_progress
from nycti.timing import elapsed_ms


def select_fresh_tool_calls(
    run: AgentRun,
    tool_calls: Sequence[object],
    *,
    available_tool_names: set[str],
    metrics: dict[str, int | str] | None,
) -> list[object]:
    fresh_calls: list[object] = []
    batch_signatures: set[str] = set()
    for tool_call in tool_calls:
        name = str(getattr(tool_call, "name", ""))
        arguments = str(getattr(tool_call, "arguments", ""))
        if name not in available_tool_names:
            append_skipped_tool_result(
                run,
                tool_call,
                reason="Rejected because this tool was not authorized for the current request.",
            )
            increment_metric(metrics, "unauthorized_tool_call_count")
            continue
        signature = tool_call_signature(name, arguments)
        if signature in run.seen_tool_signatures or signature in batch_signatures:
            append_skipped_tool_result(
                run,
                tool_call,
                reason="Skipped exact duplicate tool call; use the earlier result.",
            )
            increment_metric(metrics, "duplicate_tool_call_count")
            continue
        if name == GET_CHANNEL_CONTEXT_TOOL_NAME and (
            name in run.attempted_tools
            or any(str(getattr(call, "name", "")) == name for call in fresh_calls)
        ):
            append_skipped_tool_result(
                run,
                tool_call,
                reason=(
                    "Skipped because channel context is limited to one bounded read per response. "
                    "Use the context already returned or ask one narrow clarification."
                ),
            )
            increment_metric(metrics, "repeated_channel_context_call_count")
            continue
        batch_signatures.add(signature)
        fresh_calls.append(tool_call)
    return fresh_calls


def record_executable_tool_calls(run: AgentRun, tool_calls: Sequence[object]) -> None:
    for tool_call in tool_calls:
        name = str(getattr(tool_call, "name", ""))
        arguments = str(getattr(tool_call, "arguments", ""))
        run.seen_tool_signatures.add(tool_call_signature(name, arguments))


async def execute_tool_batch(
    *,
    run: AgentRun,
    tool_runner: object,
    executable_calls: Sequence[object],
    budget_selection: object,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int,
    source_message_id: int | None,
    request_text: str,
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
    progress: ResponseProgressReporter | None,
) -> list[ToolOutcome] | None:
    run.step = AgentStep.TOOLS
    await advance_response_progress(
        progress,
        ResponseProgressPhase.TOOLS,
        tool_names=[str(getattr(call, "name", "")) for call in executable_calls],
    )
    started_at = time.perf_counter()
    try:
        outcomes = await asyncio.wait_for(
            tool_runner.run(
                executable_calls,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                permissions=run.permissions,
                run_id=run.run_id,
                step_index=run.model_turns,
            ),
            timeout=max(run.work_seconds_remaining(), 0.001),
        )
    except TimeoutError:
        budget_selection.record_execution(run)
        run.attempted_tools.update(
            str(getattr(tool_call, "name", "")) for tool_call in executable_calls
        )
        append_unresolved_tool_results(
            run,
            executable_calls,
            (),
            metrics=metrics,
            reason=(
                "Tool execution timed out before a result was available. "
                "Continue from the other available context."
            ),
        )
        run.stop_reason = StopReason.DEADLINE
        return None
    finally:
        if metrics is not None:
            metrics["tool_execution_wall_ms"] = int(
                metrics.get("tool_execution_wall_ms", 0)
            ) + elapsed_ms(started_at)

    reconciled = reconcile_tool_outcomes(executable_calls, outcomes, metrics=metrics)
    budget_selection.record_execution(run, reconciled)
    run.attempted_tools.update(
        str(getattr(tool_call, "name", "")) for tool_call in executable_calls
    )
    run.successful_tools.update(
        outcome.tool_name for outcome in reconciled if outcome.status == ToolStatus.OK
    )
    append_tool_outcomes(run, reconciled, metrics=metrics, trace=trace)
    append_unresolved_tool_results(
        run,
        executable_calls,
        reconciled,
        metrics=metrics,
        reason=(
            "Tool execution returned no result for this call. "
            "Continue from the other available context."
        ),
    )
    append_evidence_guidance(run, metrics=metrics, request_text=request_text)
    for outcome in reconciled:
        run.add_step_record(
            state=AgentStep.TOOLS,
            tool_name=outcome.tool_name,
            argument_hash=hashlib.sha256(outcome.arguments.encode()).hexdigest(),
            status=str(outcome.status),
            latency_ms=outcome.latency_ms,
            details={
                "retryable": outcome.retryable,
                "provenance": list(outcome.provenance),
                "batch_index": run.model_turns,
                "argument_fields": list(argument_field_names(outcome.arguments)),
            },
        )
    return reconciled


def reconcile_tool_outcomes(
    tool_calls: Sequence[object],
    outcomes: Sequence[ToolOutcome],
    *,
    metrics: dict[str, int | str] | None,
) -> list[ToolOutcome]:
    expected_by_id = {
        str(getattr(tool_call, "id", "")): tool_call
        for tool_call in tool_calls
        if str(getattr(tool_call, "id", ""))
    }
    matched_by_id: dict[str, ToolOutcome] = {}
    for outcome in outcomes:
        expected_call = expected_by_id.get(outcome.call_id)
        if (
            expected_call is None
            or outcome.call_id in matched_by_id
            or outcome.tool_name != str(getattr(expected_call, "name", ""))
        ):
            increment_metric(metrics, "invalid_tool_outcome_count")
            continue
        matched_by_id[outcome.call_id] = outcome
    ordered: list[ToolOutcome] = []
    appended_call_ids: set[str] = set()
    for tool_call in tool_calls:
        call_id = str(getattr(tool_call, "id", ""))
        if call_id in matched_by_id and call_id not in appended_call_ids:
            ordered.append(matched_by_id[call_id])
            appended_call_ids.add(call_id)
    return ordered


def append_unresolved_tool_results(
    run: AgentRun,
    tool_calls: Sequence[object],
    outcomes: Sequence[ToolOutcome],
    *,
    metrics: dict[str, int | str] | None,
    reason: str,
) -> None:
    resolved_call_ids = {outcome.call_id for outcome in outcomes}
    appended_call_ids: set[str] = set()
    for tool_call in tool_calls:
        call_id = str(getattr(tool_call, "id", ""))
        if not call_id or call_id in resolved_call_ids or call_id in appended_call_ids:
            continue
        append_skipped_tool_result(run, tool_call, reason=reason)
        appended_call_ids.add(call_id)
        increment_metric(metrics, "missing_tool_outcome_count")
