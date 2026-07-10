from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import TYPE_CHECKING, Any

from nycti.agent_trace import AgentTrace
from nycti.chat.loop_messages import build_assistant_turn_message, fallback_text
from nycti.chat.orchestrator_support import (
    collect_reasoning,
    increment_metric,
    join_continuation_parts,
    looks_like_raw_tavily_dump,
    looks_like_tool_call_markup,
    should_continue_answer,
)
from nycti.chat.run_state import AgentRun, AgentStep

if TYPE_CHECKING:
    from nycti.llm.client import LLMChatTurn

LOGGER = logging.getLogger(__name__)
ModelCall = Callable[..., Awaitable[Any]]


async def finalize_run(
    *,
    run: AgentRun,
    call_model: ModelCall,
    chat_model: str,
    final_max_tokens: int,
    continuation_max_tokens: int,
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
) -> tuple[str, list[str]]:
    run.step = AgentStep.FINALIZE
    run.messages.append(
        {
            "role": "user",
            "content": (
                "Stop using tools. Give the best concise final answer from the context and tool results already "
                "available. Prefer current dated provider evidence over prior model knowledge or older speculative "
                "sources. State uncertainty briefly when evidence is incomplete. Do not paste raw tool output."
            ),
        }
    )
    if run.final_seconds_remaining() <= 0:
        _record_final_failure(run, metrics, reason="no_time_remaining")
        return fallback_text(run), []
    try:
        turn = await call_model(
            run=run,
            chat_model=chat_model,
            feature="chat_reply_final",
            max_tokens=final_max_tokens,
            temperature=0.4,
            tools=None,
            timeout_seconds=run.final_seconds_remaining(),
            metrics=metrics,
            trace=trace,
        )
    except TimeoutError:
        _record_final_failure(run, metrics, reason="timeout")
        return fallback_text(run), []
    except Exception as exc:
        LOGGER.warning("Final chat turn failed: %s", exc)
        _record_final_failure(run, metrics, reason="provider_error", detail=_summarize_exception(exc))
        return fallback_text(run), []

    reasoning = collect_reasoning(turn)
    if not turn.text:
        increment_metric(metrics, "chat_empty_final_count")
        _record_final_failure(run, metrics, reason="empty")
        return fallback_text(run), reasoning
    if looks_like_raw_tavily_dump(turn.text):
        _record_final_failure(run, metrics, reason="raw_output", detail="tavily_dump")
        return fallback_text(run, turn.text), reasoning
    if looks_like_tool_call_markup(turn.text):
        _record_final_failure(run, metrics, reason="raw_output", detail="tool_call_markup")
        return fallback_text(run, turn.text), reasoning
    answer, continuation_reasoning = await continue_once_if_needed(
        run=run,
        call_model=call_model,
        chat_model=chat_model,
        messages=run.messages,
        initial_turn=turn,
        initial_max_tokens=final_max_tokens,
        continuation_max_tokens=continuation_max_tokens,
        metrics=metrics,
        trace=trace,
    )
    reasoning.extend(continuation_reasoning)
    run.final_status = "recovered"
    return answer, reasoning


def _record_final_failure(
    run: AgentRun,
    metrics: dict[str, int | str] | None,
    *,
    reason: str,
    detail: str = "",
) -> None:
    run.final_status = "fallback"
    run.final_failure_reason = detail or reason
    increment_metric(metrics, "chat_final_failure_count")
    if metrics is None:
        return
    metrics["chat_final_failure_reason"] = reason
    if detail:
        key = "chat_final_raw_output_kind" if reason == "raw_output" else "chat_final_failure_error"
        metrics[key] = detail


def _summarize_exception(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if len(text) > 160:
        text = text[:157].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


async def continue_once_if_needed(
    *,
    run: AgentRun,
    call_model: ModelCall,
    chat_model: str,
    messages: list[dict[str, object]],
    initial_turn: LLMChatTurn,
    initial_max_tokens: int,
    continuation_max_tokens: int,
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
) -> tuple[str, list[str]]:
    if (
        not should_continue_answer(initial_turn, max_tokens=initial_max_tokens)
        or run.continuations >= run.budget.max_continuations
        or run.final_seconds_remaining() <= 0
    ):
        return initial_turn.text, []
    run.continuations += 1
    increment_metric(metrics, "chat_length_finish_count")
    continuation_messages = [
        *messages,
        build_assistant_turn_message(initial_turn),
        {
            "role": "user",
            "content": "Continue exactly where the answer stopped. Do not restart or repeat earlier content.",
        },
    ]
    original_messages = run.messages
    run.messages = continuation_messages
    try:
        turn = await call_model(
            run=run,
            chat_model=chat_model,
            feature="chat_reply_continuation",
            max_tokens=continuation_max_tokens,
            temperature=0.4,
            tools=None,
            timeout_seconds=run.final_seconds_remaining(),
            metrics=metrics,
            trace=trace,
        )
    except TimeoutError:
        return initial_turn.text, []
    finally:
        run.messages = original_messages
    increment_metric(metrics, "chat_continuation_count")
    if not turn.text or looks_like_tool_call_markup(turn.text):
        return initial_turn.text, collect_reasoning(turn)
    return join_continuation_parts([initial_turn.text, turn.text]), collect_reasoning(turn)
