from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, call

from nycti.llm.quota_execution import complete_chat_turn_with_quota
from nycti.llm.token_quota import TokenQuotaRoute
from nycti.llm.types import LLMChatTurn, LLMProviderAttempt, LLMUsage


TERRA = "gpt-5.6-terra"
LUNA = "gpt-5.6-luna"


class _FakeQuota:
    provider = "openai"
    fallback_model = LUNA
    fallback_reasoning_effort = "high"

    def __init__(self, route: TokenQuotaRoute) -> None:
        self._route = route
        self.route_calls: list[tuple[str, dict[str, object]]] = []
        self.settle_calls: list[tuple[TokenQuotaRoute, int]] = []
        self.release_calls: list[TokenQuotaRoute] = []
        self.uncertain_calls: list[TokenQuotaRoute] = []
        self.exhausted_calls: list[tuple[str, TokenQuotaRoute | None]] = []

    async def route(self, model: str, **kwargs: object) -> TokenQuotaRoute:
        self.route_calls.append((model, kwargs))
        return self._route

    async def settle(self, route: TokenQuotaRoute, total_tokens: int) -> bool:
        self.settle_calls.append((route, total_tokens))
        return True

    async def release(self, route: TokenQuotaRoute) -> bool:
        self.release_calls.append(route)
        return True

    async def charge_uncertain(self, route: TokenQuotaRoute) -> bool:
        self.uncertain_calls.append(route)
        return True

    async def mark_exhausted(
        self,
        model: str,
        route: TokenQuotaRoute | None = None,
    ) -> bool:
        self.exhausted_calls.append((model, route))
        return True


def _route(
    *,
    model: str = TERRA,
    reservation_id: str | None = "reservation-1",
    fallback_reason: str | None = None,
    reasoning_effort: str | None = None,
) -> TokenQuotaRoute:
    return TokenQuotaRoute(
        requested_model=TERRA,
        model=model,
        provider="openai",
        reasoning_effort=reasoning_effort,
        reservation_id=reservation_id,
        reserved_tokens=512 if reservation_id else 0,
        fallback_reason=fallback_reason,
    )


def _turn(
    *,
    model: str = TERRA,
    provider: str = "openai",
    total_tokens: int = 123,
    provider_attempts: list[LLMProviderAttempt] | None = None,
) -> LLMChatTurn:
    return LLMChatTurn(
        text="answer",
        raw_text="answer",
        usage=LLMUsage(
            feature="chat",
            model=model,
            prompt_tokens=max(0, total_tokens - 23),
            completion_tokens=min(23, total_tokens),
            total_tokens=total_tokens,
            estimated_cost_usd=0.0,
            provider=provider,
        ),
        tool_calls=[],
        reasoning_content="",
        finish_reason="stop",
        provider_attempts=provider_attempts or [],
    )


async def _execute(
    *,
    quota: _FakeQuota | None,
    complete: Any,
    **overrides: object,
) -> LLMChatTurn:
    kwargs: dict[str, object] = {
        "quota": quota,
        "complete": complete,
        "model": TERRA,
        "feature": "chat",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 256,
        "temperature": 0.2,
        "tools": [{"type": "function", "function": {"name": "web_search"}}],
        "use_native_tools": True,
        "reasoning_effort_override": "medium",
        "request_timeout_seconds": 15.0,
        "request_max_retries": 2,
    }
    kwargs.update(overrides)
    return await complete_chat_turn_with_quota(**kwargs)  # type: ignore[arg-type]


class QuotaExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_quota_passes_request_and_turn_through_unchanged(self) -> None:
        turn = _turn()
        complete = AsyncMock(return_value=turn)

        result = await _execute(quota=None, complete=complete)

        self.assertIs(result, turn)
        self.assertEqual(result.usage.requested_model, "")
        complete.assert_awaited_once_with(
            model=TERRA,
            feature="chat",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=256,
            temperature=0.2,
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            use_native_tools=True,
            reasoning_effort_override="medium",
            request_timeout_seconds=15.0,
            request_max_retries=2,
        )

    async def test_local_budget_fallback_uses_luna_high_and_preserves_request(self) -> None:
        route = _route(
            model=LUNA,
            reservation_id=None,
            fallback_reason="daily_limit_reached",
            reasoning_effort="high",
        )
        quota = _FakeQuota(route)
        complete = AsyncMock(return_value=_turn(model=LUNA))

        result = await _execute(quota=quota, complete=complete)

        self.assertEqual(result.usage.model, LUNA)
        self.assertEqual(result.usage.requested_model, TERRA)
        self.assertEqual(complete.await_args.kwargs["model"], LUNA)
        self.assertEqual(
            complete.await_args.kwargs["reasoning_effort_override"],
            "high",
        )
        self.assertEqual(complete.await_args.kwargs["request_max_retries"], 2)
        self.assertEqual(quota.settle_calls, [])

    async def test_reserved_terra_success_settles_usage_and_disables_sdk_retries(self) -> None:
        route = _route()
        quota = _FakeQuota(route)
        complete = AsyncMock(return_value=_turn(total_tokens=321))

        result = await _execute(quota=quota, complete=complete)

        self.assertEqual(quota.settle_calls, [(route, 321)])
        self.assertEqual(quota.release_calls, [])
        self.assertEqual(quota.uncertain_calls, [])
        self.assertEqual(complete.await_args.kwargs["request_max_retries"], 0)
        self.assertEqual(result.usage.requested_model, TERRA)

    async def test_deterministic_provider_error_releases_reservation(self) -> None:
        route = _route()
        quota = _FakeQuota(route)
        complete = AsyncMock(side_effect=ValueError("invalid model"))

        with self.assertRaisesRegex(ValueError, "invalid model"):
            await _execute(quota=quota, complete=complete)

        self.assertEqual(quota.release_calls, [route])
        self.assertEqual(quota.settle_calls, [])
        self.assertEqual(quota.uncertain_calls, [])

    async def test_timeout_and_cancellation_charge_uncertain_reservation(self) -> None:
        for error in (TimeoutError("request timed out"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                route = _route()
                quota = _FakeQuota(route)
                complete = AsyncMock(side_effect=error)

                with self.assertRaises(type(error)):
                    await _execute(quota=quota, complete=complete)

                self.assertEqual(quota.uncertain_calls, [route])
                self.assertEqual(quota.release_calls, [])
                self.assertEqual(quota.settle_calls, [])

    async def test_provider_quota_exhaustion_marks_model_and_retries_luna_high(self) -> None:
        route = _route()
        quota = _FakeQuota(route)
        luna_turn = _turn(model=LUNA)
        complete = AsyncMock(
            side_effect=[RuntimeError("daily quota exceeded"), luna_turn]
        )

        result = await _execute(quota=quota, complete=complete)

        self.assertIs(result, luna_turn)
        self.assertEqual(result.usage.requested_model, TERRA)
        self.assertEqual(quota.exhausted_calls, [(TERRA, route)])
        self.assertEqual(complete.await_count, 2)
        self.assertEqual(
            complete.await_args_list,
            [
                call(
                    model=TERRA,
                    feature="chat",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=256,
                    temperature=0.2,
                    tools=[
                        {"type": "function", "function": {"name": "web_search"}}
                    ],
                    use_native_tools=True,
                    reasoning_effort_override="medium",
                    request_timeout_seconds=15.0,
                    request_max_retries=0,
                ),
                call(
                    model=LUNA,
                    feature="chat",
                    messages=[{"role": "user", "content": "hello"}],
                    max_tokens=256,
                    temperature=0.2,
                    tools=[
                        {"type": "function", "function": {"name": "web_search"}}
                    ],
                    use_native_tools=True,
                    reasoning_effort_override="high",
                    request_timeout_seconds=15.0,
                    request_max_retries=0,
                ),
            ],
        )

    async def test_cross_provider_success_releases_after_releasable_primary_error(
        self,
    ) -> None:
        route = _route()
        quota = _FakeQuota(route)
        fallback_turn = _turn(
            model="deepseek-ai/DeepSeek-V4-Pro",
            provider="deepinfra",
            provider_attempts=[
                LLMProviderAttempt(
                    attempt=1,
                    provider="openai",
                    model=TERRA,
                    status="error",
                    latency_ms=10,
                    native_tools=True,
                    error="invalid model",
                ),
                LLMProviderAttempt(
                    attempt=2,
                    provider="deepinfra",
                    model="deepseek-ai/DeepSeek-V4-Pro",
                    status="success",
                    latency_ms=20,
                    native_tools=True,
                ),
            ],
        )
        complete = AsyncMock(return_value=fallback_turn)

        result = await _execute(quota=quota, complete=complete)

        self.assertIs(result, fallback_turn)
        self.assertEqual(result.usage.requested_model, TERRA)
        self.assertEqual(quota.release_calls, [route])
        self.assertEqual(quota.settle_calls, [])
        self.assertEqual(quota.uncertain_calls, [])


if __name__ == "__main__":
    unittest.main()
