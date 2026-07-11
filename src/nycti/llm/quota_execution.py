from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nycti.llm.provider_policy import ProviderErrorKind, classify_provider_error
from nycti.llm.token_quota import DailyTokenQuota, TokenQuotaRoute
from nycti.llm.types import LLMChatTurn

LOGGER = logging.getLogger(__name__)

_RELEASABLE_ERROR_KINDS = frozenset(
    {
        ProviderErrorKind.TOOL_INCOMPATIBLE,
        ProviderErrorKind.AUTHENTICATION,
        ProviderErrorKind.DEPLOYMENT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.ACCESS_DENIED,
        ProviderErrorKind.INVALID_REQUEST,
    }
)


async def complete_chat_turn_with_quota(
    *,
    quota: DailyTokenQuota | None,
    complete: Callable[..., Awaitable[LLMChatTurn]],
    model: str,
    feature: str,
    messages: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
    tools: list[dict[str, object]] | None,
    use_native_tools: bool,
    reasoning_effort_override: str | None,
    request_timeout_seconds: float | None,
    request_max_retries: int | None,
) -> LLMChatTurn:
    call_kwargs: dict[str, Any] = {
        "model": model,
        "feature": feature,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "tools": tools,
        "use_native_tools": use_native_tools,
        "reasoning_effort_override": reasoning_effort_override,
        "request_timeout_seconds": request_timeout_seconds,
        "request_max_retries": request_max_retries,
    }
    if quota is None:
        return await complete(**call_kwargs)

    route = await quota.route(
        model,
        messages=messages,
        tools=tools or (),
        max_tokens=max_tokens,
    )
    call_kwargs["model"] = route.model
    if route.reasoning_effort is not None:
        call_kwargs["reasoning_effort_override"] = route.reasoning_effort
    if route.reservation_id is not None:
        # Hidden SDK retries can spend tokens more than once under one reservation.
        call_kwargs["request_max_retries"] = 0
    if route.fallback_reason:
        LOGGER.info(
            "Daily token route switched feature=%s requested_model=%s active_model=%s reason=%s.",
            feature,
            model,
            route.model,
            route.fallback_reason,
        )

    try:
        turn = await complete(**call_kwargs)
    except BaseException as exc:
        if route.reservation_id is not None:
            fallback_turn = await _handle_failed_reserved_call(
                quota=quota,
                route=route,
                exc=exc,
                complete=complete,
                call_kwargs=call_kwargs,
            )
            if fallback_turn is not None:
                fallback_turn.usage.requested_model = model
                return fallback_turn
        raise

    if route.reservation_id is not None:
        if _used_reserved_provider(turn, route=route, quota=quota):
            await quota.settle(route, turn.usage.total_tokens)
        elif _primary_failure_was_releasable(turn, route=route):
            await quota.release(route)
        else:
            await quota.charge_uncertain(route)
    turn.usage.requested_model = model
    return turn


async def _handle_failed_reserved_call(
    *,
    quota: DailyTokenQuota,
    route: TokenQuotaRoute,
    exc: BaseException,
    complete: Callable[..., Awaitable[LLMChatTurn]],
    call_kwargs: dict[str, Any],
) -> LLMChatTurn | None:
    kind = (
        classify_provider_error(exc)
        if isinstance(exc, Exception)
        else ProviderErrorKind.TRANSIENT
    )
    if kind == ProviderErrorKind.QUOTA_EXHAUSTED:
        await quota.mark_exhausted(route.model, route=route)
        fallback_model = quota.fallback_model
        if fallback_model and fallback_model.casefold() != route.model.casefold():
            fallback_kwargs = dict(call_kwargs)
            fallback_kwargs["model"] = fallback_model
            fallback_kwargs["reasoning_effort_override"] = quota.fallback_reasoning_effort
            fallback_kwargs["request_max_retries"] = 0
            return await complete(**fallback_kwargs)
    elif kind in _RELEASABLE_ERROR_KINDS:
        await quota.release(route)
    else:
        await quota.charge_uncertain(route)
    return None


def _used_reserved_provider(
    turn: LLMChatTurn,
    *,
    route: TokenQuotaRoute,
    quota: DailyTokenQuota,
) -> bool:
    usage = turn.usage
    if str(usage.provider).casefold() != quota.provider.casefold():
        return False
    return str(usage.model).casefold() == route.model.casefold()


def _primary_failure_was_releasable(
    turn: LLMChatTurn,
    *,
    route: TokenQuotaRoute,
) -> bool:
    attempts = [
        attempt
        for attempt in turn.provider_attempts
        if attempt.model.casefold() == route.model.casefold() and attempt.status == "error"
    ]
    return bool(attempts) and all(
        classify_provider_error(Exception(attempt.error)) in _RELEASABLE_ERROR_KINDS
        for attempt in attempts
    )
