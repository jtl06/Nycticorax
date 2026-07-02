from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from nycti.chat.orchestrator_support import (
    append_raw_tool_trace,
    increment_metric,
)
from nycti.llm.provider_policy import ProviderErrorKind, classify_provider_error
from nycti.timing import elapsed_ms

if TYPE_CHECKING:
    from nycti.agent_trace import AgentTrace
    from nycti.chat.run_state import AgentRun
    from nycti.llm.client import LLMChatTurn, OpenAIClient

MAX_AGENT_MODEL_REQUEST_TIMEOUT_SECONDS = 15.0


async def call_agent_model(
    *,
    llm_client: OpenAIClient,
    run: AgentRun,
    chat_model: str,
    feature: str,
    max_tokens: int,
    temperature: float,
    tools: list[dict[str, object]] | None,
    timeout_seconds: float,
    metrics: dict[str, int | str] | None,
    trace: AgentTrace,
) -> LLMChatTurn:
    started_at = time.perf_counter()
    try:
        turn = await asyncio.wait_for(
            llm_client.complete_chat_turn(
                model=chat_model,
                feature=feature,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=run.messages,
                tools=tools,
                use_native_tools=run.native_tools_enabled,
                request_timeout_seconds=min(
                    timeout_seconds,
                    MAX_AGENT_MODEL_REQUEST_TIMEOUT_SECONDS,
                ),
            ),
            timeout=max(timeout_seconds, 0.001),
        )
    except Exception as exc:
        error_kind = (
            ProviderErrorKind.TRANSIENT
            if isinstance(exc, TimeoutError)
            else classify_provider_error(exc)
        )
        run.add_step_record(
            state=run.step,
            feature=feature,
            requested_model=chat_model,
            provider=str(
                getattr(getattr(llm_client, "provider_capabilities", None), "name", "")
            ),
            status="timeout" if isinstance(exc, TimeoutError) else "error",
            latency_ms=elapsed_ms(started_at),
            details={
                "error_kind": str(error_kind),
                "error": " ".join(str(exc).split())[:240],
            },
        )
        raise
    run.model_turns += 1
    run.usage_records.append(turn.usage)
    if turn.native_tool_calling_failed and run.native_tools_enabled:
        run.native_tools_enabled = False
        increment_metric(metrics, "native_tool_fallback_count")
        if metrics is not None:
            metrics["provider_recovery_notice"] = (
                "native tool request was rejected; switched to plain/XML tool fallback"
            )
            if turn.native_tool_failure_request_json:
                metrics["provider_recovery_request_json"] = turn.native_tool_failure_request_json
    turn_ms = elapsed_ms(started_at)
    trace.add(
        {
            "chat_reply_final": "chat_final",
            "chat_reply_continuation": "chat_continuation",
        }.get(feature, "chat_turn"),
        elapsed_ms=turn_ms,
        attrs={
            "model": turn.usage.model,
            "feature": turn.usage.feature,
            "tokens": turn.usage.total_tokens,
            "tool_calls": len(turn.tool_calls),
        },
    )
    if metrics is not None:
        metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + turn_ms
        metrics["chat_prompt_tokens"] = (
            int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
        )
        metrics["chat_completion_tokens"] = (
            int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
        )
        metrics["chat_total_tokens"] = (
            int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
        )
        metrics["active_chat_model"] = turn.usage.model
        append_raw_tool_trace(metrics, turn.raw_text)
    run.add_step_record(
        state=run.step,
        feature=feature,
        requested_model=chat_model,
        active_model=turn.usage.model,
        provider=str(getattr(turn.usage, "provider", "")),
        attempt=int(getattr(turn.usage, "attempt", 1)),
        status="ok",
        latency_ms=elapsed_ms(started_at),
        prompt_tokens=turn.usage.prompt_tokens,
        completion_tokens=turn.usage.completion_tokens,
        total_tokens=turn.usage.total_tokens,
        details={
            "tool_calls": len(turn.tool_calls),
            "finish_reason": turn.finish_reason,
            "native_tools": run.native_tools_enabled,
        },
    )
    return turn
