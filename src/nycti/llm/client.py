from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from nycti.config import Settings
from nycti.llm.provider_policy import (
    ProviderCapabilities,
    ProviderErrorKind,
    capabilities_for_base_url,
    classify_provider_error,
    failover_cooldown_seconds,
)
from nycti.llm.provider_attempts import (
    is_deterministic_model_unavailable_error as _is_deterministic_model_unavailable_error,
    is_transient_provider_error as is_transient_provider_error,
    offset_provider_attempts as _offset_provider_attempts,
    provider_label as _provider_label,
    record_last_parse_timing as _record_last_parse_timing,
    should_fail_over_chat_model as _should_fail_over_chat_model,
    should_retry_busy_foreground_chat as _should_retry_busy_foreground_chat,
    summarize_provider_error as _summarize_provider_error,
)
from nycti.llm.provider_settings import FallbackProviderSettings
from nycti.llm.quota_execution import complete_chat_turn_with_quota
from nycti.llm.reasoning import (
    reasoning_effort_for_feature as _reasoning_effort_for_feature,
    reasoning_effort_for_model as _reasoning_effort_for_model,
)
from nycti.llm.responses_adapter import (
    RESPONSES_OUTPUT_ITEMS_KEY,
    build_responses_request,
    parse_responses_turn,
    should_use_responses_api,
)
from nycti.llm.tool_calls import LLMToolCall
from nycti.llm.types import (
    DEFAULT_PRICING,
    EmbeddingResult,
    LLMChatTurn,
    LLMProviderAttempt,
    LLMResult,
    LLMUsage,
    ModelPricing,
)
from nycti.llm.token_quota import DailyTokenQuota
from nycti.llm.transport import (
    LLMTransport,
    OpenAISDKTransport,
    transport_timing,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
ECONOMY_ONLY_FEATURES = frozenset({"ambient_addressedness", "deep_research_plan", "deep_research_reduce"})


class OpenAIClient:
    def __init__(
        self,
        settings: Settings,
        *,
        token_quota: DailyTokenQuota | None = None,
        transport: LLMTransport | None = None,
        client_factory: Callable[..., Any] = AsyncOpenAI,
    ) -> None:
        self.settings = settings
        self.token_quota = token_quota
        self.transport = transport or OpenAISDKTransport()
        # Pass the official endpoint explicitly so blank OPENAI_BASE_URL cannot become a relative SDK URL.
        client_kwargs = {"api_key": settings.openai_api_key,
                         "base_url": settings.openai_base_url or DEFAULT_OPENAI_BASE_URL}
        self.client = client_factory(**client_kwargs)
        embedding_client_kwargs = {"api_key": settings.openai_embedding_api_key or settings.openai_api_key,
                                   "base_url": DEFAULT_OPENAI_BASE_URL}
        if settings.openai_embedding_base_url:
            embedding_client_kwargs["base_url"] = settings.openai_embedding_base_url
        elif settings.openai_embedding_api_key is None and settings.openai_base_url:
            embedding_client_kwargs["base_url"] = settings.openai_base_url
        self.embedding_client = client_factory(**embedding_client_kwargs)
        self.provider_capabilities = capabilities_for_base_url(settings.openai_base_url)
        self._unhealthy_chat_models_until: dict[str, float] = {}
        self.fallback_client: OpenAIClient | None = None
        fallback_api_key = str(getattr(settings, "openai_fallback_api_key", "") or "").strip()
        fallback_base_url = str(getattr(settings, "openai_fallback_base_url", "") or "").strip()
        fallback_model = str(getattr(settings, "openai_fallback_chat_model", "") or "").strip()
        if fallback_api_key and fallback_base_url and fallback_model:
            self.fallback_client = OpenAIClient(
                FallbackProviderSettings(
                    openai_api_key=fallback_api_key,
                    openai_base_url=fallback_base_url,
                    openai_chat_model=fallback_model,
                ),
                client_factory=client_factory,
                transport=self.transport,
            )

    async def complete_chat(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        reasoning_effort_override: str | None = None,
        request_timeout_seconds: float | None = None,
        request_max_retries: int | None = None,
    ) -> LLMResult:
        result = await self.complete_chat_turn(
            model=model,
            feature=feature,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort_override=reasoning_effort_override,
            request_timeout_seconds=request_timeout_seconds,
            request_max_retries=request_max_retries,
        )
        return LLMResult(text=result.text, usage=result.usage)

    async def create_embedding(
        self,
        *,
        model: str,
        feature: str,
        text: str,
    ) -> EmbeddingResult:
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ValueError("Embedding text must not be blank.")
        response = await self.embedding_client.embeddings.create(
            model=model,
            input=cleaned_text,
        )
        data = response.data[0]
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else prompt_tokens
        return EmbeddingResult(
            embedding=[float(value) for value in data.embedding],
            usage=LLMUsage(
                feature=feature,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=total_tokens,
                estimated_cost_usd=self._estimate_cost(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                ),
            ),
        )

    async def complete_chat_turn(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None = None,
        use_native_tools: bool = True,
        reasoning_effort_override: str | None = None,
        request_timeout_seconds: float | None = None,
        request_max_retries: int | None = None,
    ) -> LLMChatTurn:
        quota = self.token_quota if self.provider_capabilities.name == "openai" else None
        return await complete_chat_turn_with_quota(
            quota=quota, complete=self._complete_chat_turn_unmetered,
            model=model, feature=feature, messages=messages, max_tokens=max_tokens,
            temperature=temperature, tools=tools, use_native_tools=use_native_tools,
            reasoning_effort_override=reasoning_effort_override,
            request_timeout_seconds=request_timeout_seconds,
            request_max_retries=request_max_retries,
        )

    async def _complete_chat_turn_unmetered(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None = None,
        use_native_tools: bool = True,
        reasoning_effort_override: str | None = None,
        request_timeout_seconds: float | None = None,
        request_max_retries: int | None = None,
    ) -> LLMChatTurn:
        if should_use_responses_api(
            provider_name=self.provider_capabilities.name,
            model=model,
        ):
            return await self._complete_responses_turn(
                model=model,
                feature=feature,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                use_native_tools=use_native_tools,
                reasoning_effort_override=reasoning_effort_override,
                request_timeout_seconds=request_timeout_seconds,
                request_max_retries=request_max_retries,
            )
        messages = _without_responses_output_items(messages)
        completion = None
        actual_model = model
        last_error: Exception | None = None
        candidate_models = self._chat_model_candidates(model, feature=feature)
        provider_attempts: list[LLMProviderAttempt] = []
        attempt_number = 0
        native_tools_requested = bool(tools and use_native_tools)
        native_tools_allowed = native_tools_requested and self.provider_capabilities.native_tools
        request_messages = messages
        if not candidate_models:
            if self._can_use_cross_provider_fallback(model=model, feature=feature):
                return await self._complete_cross_provider_fallback(
                    requested_model=model,
                    feature=feature,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    use_native_tools=use_native_tools,
                    reasoning_effort_override=reasoning_effort_override,
                    request_timeout_seconds=request_timeout_seconds,
                    request_max_retries=request_max_retries,
                    prior_attempts=attempt_number,
                    prior_provider_attempts=provider_attempts,
                )
            raise RuntimeError(f"All configured candidates for chat model {model!r} are temporarily unavailable.")
        LOGGER.info(
            "Chat completion start feature=%s provider=%s requested_model=%s candidates=%s native_tools=%s tool_count=%s message_count=%s.",
            feature,
            _provider_label(self.settings.openai_base_url),
            model,
            " -> ".join(candidate_models),
            "yes" if native_tools_allowed else "no",
            len(tools or []),
            len(request_messages),
        )
        for candidate_index, candidate_model in enumerate(candidate_models):
            try:
                request_kwargs = _build_chat_completion_request(
                    model=candidate_model,
                    messages=request_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    capabilities=self.provider_capabilities,
                    reasoning_effort=_reasoning_effort_for_feature(
                        feature=feature,
                        foreground_effort=str(getattr(self.settings, "openai_reasoning_effort", "") or ""),
                        efficiency_effort=str(getattr(self.settings, "openai_efficiency_reasoning_effort", "") or ""),
                        override=reasoning_effort_override,
                    ),
                )
                if native_tools_allowed:
                    request_kwargs["tools"] = tools
                # One optional busy retry; never change tools or discard context to obtain a response.
                for retry in range(2):
                    attempt_number += 1
                    try:
                        completion = await self._create_tracked_chat_completion(
                            request_kwargs, model=candidate_model, attempts=provider_attempts,
                            request_timeout_seconds=request_timeout_seconds,
                            request_max_retries=request_max_retries,
                        )
                        break
                    except Exception as exc:
                        _attach_debug_request(exc, request_kwargs)
                        if retry == 0 and _should_retry_busy_foreground_chat(feature, exc):
                            await asyncio.sleep(1.0)
                            continue
                        raise
                actual_model = candidate_model
                self._clear_chat_model_cooldown(candidate_model)
            except Exception as exc:
                last_error = exc
                error_kind = classify_provider_error(exc)
                if _is_deterministic_model_unavailable_error(exc):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                if (
                    candidate_index + 1 < len(candidate_models)
                    and _should_fail_over_chat_model(exc)
                ):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                    LOGGER.warning(
                        "Chat model %s failed with a model-level provider error; falling back to %s. error=%s",
                        candidate_model,
                        candidate_models[candidate_index + 1],
                        _summarize_provider_error(exc),
                    )
                    continue
                if (
                    _should_fail_over_chat_model(exc)
                    and self._can_use_cross_provider_fallback(model=model, feature=feature)
                ):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                    return await self._complete_cross_provider_fallback(
                        requested_model=model,
                        feature=feature,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        use_native_tools=use_native_tools,
                        reasoning_effort_override=reasoning_effort_override,
                        request_timeout_seconds=request_timeout_seconds,
                        request_max_retries=request_max_retries,
                        prior_attempts=attempt_number,
                        prior_provider_attempts=provider_attempts,
                    )
                raise
            if completion is not None:
                break
        if completion is None:
            assert last_error is not None
            raise last_error
        assert completion is not None
        parse_started_at = time.perf_counter()
        choice = completion.choices[0]
        message = choice.message
        content = message.content or ""
        reasoning_content = getattr(message, "reasoning_content", None) or ""
        tool_calls: list[LLMToolCall] = []
        for tool_call in message.tool_calls or []:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", "")
            arguments = getattr(function, "arguments", "")
            if not name:
                continue
            tool_calls.append(
                LLMToolCall(
                    id=tool_call.id,
                    name=name,
                    arguments=arguments or "",
                )
            )
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else prompt_tokens + completion_tokens
        _record_last_parse_timing(
            provider_attempts,
            round((time.perf_counter() - parse_started_at) * 1000),
        )
        return LLMChatTurn(
            text=content.strip(),
            raw_text=(message.content or "").strip(),
            usage=LLMUsage(
                feature=feature,
                model=actual_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=self._estimate_cost(
                    model=actual_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                ),
                provider=self.provider_capabilities.name,
                requested_model=model,
                attempt=attempt_number,
            ),
            tool_calls=tool_calls,
            reasoning_content=reasoning_content.strip() if reasoning_content else "",
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            provider_attempts=provider_attempts,
        )

    def _chat_model_candidates(self, model: str, *, feature: str = "") -> list[str]:
        candidates = [model]
        if (model == self.settings.openai_chat_model and feature not in ECONOMY_ONLY_FEATURES
                and not (self.token_quota and self.token_quota.is_limited(model))):
            candidates.extend(self.settings.openai_chat_model_fallbacks)
        unique_candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
        return [candidate for candidate in unique_candidates if not self._is_chat_model_unhealthy(candidate)]

    async def _complete_responses_turn(
        self,
        *,
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
        candidates = self._chat_model_candidates(model, feature=feature)
        provider_attempts: list[LLMProviderAttempt] = []
        if not candidates:
            if self._can_use_cross_provider_fallback(model=model, feature=feature):
                return await self._complete_cross_provider_fallback(
                    requested_model=model,
                    feature=feature,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    use_native_tools=use_native_tools,
                    reasoning_effort_override=reasoning_effort_override,
                    request_timeout_seconds=request_timeout_seconds,
                    request_max_retries=request_max_retries,
                    prior_attempts=0,
                    prior_provider_attempts=provider_attempts,
                )
            raise RuntimeError(f"All configured candidates for chat model {model!r} are temporarily unavailable.")

        requested_reasoning_effort = _reasoning_effort_for_feature(
            feature=feature,
            foreground_effort=str(
                getattr(self.settings, "openai_reasoning_effort", "") or ""
            ),
            efficiency_effort=str(
                getattr(self.settings, "openai_efficiency_reasoning_effort", "") or ""
            ),
            override=reasoning_effort_override,
        )
        native_tools = tools if tools and use_native_tools else None
        for candidate_index, candidate_model in enumerate(candidates):
            reasoning_effort = _reasoning_effort_for_model(
                model=candidate_model,
                effort=requested_reasoning_effort,
            )
            request_kwargs = build_responses_request(
                model=candidate_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tools=native_tools,
                service_tier=str(
                    getattr(self.settings, "openai_service_tier", "") or ""
                )
                or None,
            )
            LOGGER.info(
                "Responses completion attempt feature=%s provider=%s model=%s candidate=%s/%s "
                "reasoning_effort=%s native_tools=%s tool_count=%s.",
                feature,
                _provider_label(self.settings.openai_base_url),
                candidate_model,
                candidate_index + 1,
                len(candidates),
                reasoning_effort or "default",
                "yes" if native_tools else "no",
                len(native_tools or []),
            )
            started_at = time.perf_counter()
            try:
                response = await self._create_response(
                    request_kwargs,
                    request_timeout_seconds=request_timeout_seconds,
                    request_max_retries=request_max_retries,
                )
                parse_started_at = time.perf_counter()
                data = parse_responses_turn(response, requested_model=candidate_model)
                parse_ms = round((time.perf_counter() - parse_started_at) * 1000)
            except Exception as exc:
                transport = transport_timing(self.transport)
                _attach_debug_request(exc, request_kwargs)
                provider_attempts.append(
                    LLMProviderAttempt(
                        attempt=len(provider_attempts) + 1,
                        provider=self.provider_capabilities.name,
                        model=candidate_model,
                        status="error",
                        latency_ms=round((time.perf_counter() - started_at) * 1000),
                        native_tools=bool(native_tools),
                        error=_summarize_provider_error(exc),
                        request_ms=transport.request_ms,
                    )
                )
                try:
                    setattr(exc, "nycti_provider_attempts", list(provider_attempts))
                except Exception:
                    pass
                error_kind = classify_provider_error(exc)
                if _is_deterministic_model_unavailable_error(exc):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                if candidate_index + 1 < len(candidates) and _should_fail_over_chat_model(exc):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                    continue
                if (
                    _should_fail_over_chat_model(exc)
                    and self._can_use_cross_provider_fallback(model=model, feature=feature)
                ):
                    self._mark_chat_model_unhealthy(candidate_model, error_kind)
                    return await self._complete_cross_provider_fallback(
                        requested_model=model,
                        feature=feature,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        use_native_tools=use_native_tools,
                        reasoning_effort_override=reasoning_effort_override,
                        request_timeout_seconds=request_timeout_seconds,
                        request_max_retries=request_max_retries,
                        prior_attempts=len(provider_attempts),
                        prior_provider_attempts=provider_attempts,
                    )
                raise

            transport = transport_timing(self.transport)
            provider_attempts.append(
                LLMProviderAttempt(
                    attempt=len(provider_attempts) + 1,
                    provider=self.provider_capabilities.name,
                    model=candidate_model,
                    status="ok",
                    latency_ms=round((time.perf_counter() - started_at) * 1000),
                    native_tools=bool(native_tools),
                    request_ms=transport.request_ms,
                    parse_ms=parse_ms,
                )
            )
            self._clear_chat_model_cooldown(candidate_model)
            return LLMChatTurn(
                text=data.text,
                raw_text=data.raw_text,
                usage=LLMUsage(
                    feature=feature,
                    model=data.model,
                    prompt_tokens=data.prompt_tokens,
                    completion_tokens=data.completion_tokens,
                    total_tokens=data.total_tokens,
                    estimated_cost_usd=self._estimate_cost(
                        model=data.model,
                        prompt_tokens=data.prompt_tokens,
                        completion_tokens=data.completion_tokens,
                    ),
                    provider=self.provider_capabilities.name,
                    requested_model=model,
                    attempt=len(provider_attempts),
                    cached_prompt_tokens=data.cached_prompt_tokens,
                    reasoning_tokens=data.reasoning_tokens,
                ),
                tool_calls=data.tool_calls,
                reasoning_content=data.reasoning_content,
                finish_reason=data.finish_reason,
                provider_attempts=provider_attempts,
                refusal=data.refusal,
                incomplete_details=data.incomplete_details,
                response_output_items=data.response_output_items,
            )
        raise RuntimeError(f"All configured Responses API candidates failed for {model!r}.")

    def _can_use_cross_provider_fallback(self, *, model: str, feature: str) -> bool:
        configured_models = {
            str(getattr(self.settings, "openai_chat_model", "") or ""),
            str(getattr(self.settings, "openai_quick_model", "") or ""),
            str(getattr(self.settings, "openai_deep_model", "") or ""),
            str(getattr(self.settings, "openai_daily_token_fallback_model", "") or ""),
            str(getattr(self.settings, "openai_memory_model", "") or ""),
            str(getattr(self.settings, "openai_vision_model", "") or ""),
        }
        if feature in ECONOMY_ONLY_FEATURES:
            return False
        return self.fallback_client is not None and model in configured_models

    async def _complete_cross_provider_fallback(
        self,
        *,
        requested_model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None,
        use_native_tools: bool,
        reasoning_effort_override: str | None,
        request_timeout_seconds: float | None,
        request_max_retries: int | None,
        prior_attempts: int,
        prior_provider_attempts: list[LLMProviderAttempt],
    ) -> LLMChatTurn:
        assert self.fallback_client is not None
        fallback_model = self.fallback_client.settings.openai_chat_model
        LOGGER.warning(
            "Primary chat provider failed feature=%s; falling back across providers to %s model=%s.",
            feature,
            _provider_label(self.fallback_client.settings.openai_base_url),
            fallback_model,
        )
        try:
            turn = await self.fallback_client.complete_chat_turn(
                model=fallback_model,
                feature=feature,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                use_native_tools=use_native_tools,
                reasoning_effort_override=reasoning_effort_override,
                request_timeout_seconds=request_timeout_seconds,
                request_max_retries=request_max_retries,
            )
        except Exception as exc:
            fallback_attempts = _offset_provider_attempts(
                getattr(exc, "nycti_provider_attempts", []),
                offset=len(prior_provider_attempts),
            )
            try:
                setattr(
                    exc,
                    "nycti_provider_attempts",
                    [*prior_provider_attempts, *fallback_attempts],
                )
            except Exception:
                pass
            raise
        turn.usage.requested_model = requested_model
        turn.usage.attempt += prior_attempts
        fallback_attempts = _offset_provider_attempts(
            turn.provider_attempts,
            offset=len(prior_provider_attempts),
        )
        turn.provider_attempts = [*prior_provider_attempts, *fallback_attempts]
        return turn

    async def _create_tracked_chat_completion(
        self,
        request_kwargs: dict[str, object],
        *,
        model: str,
        attempts: list[LLMProviderAttempt],
        request_timeout_seconds: float | None,
        request_max_retries: int | None,
    ):
        started_at = time.perf_counter()
        attempt_number = len(attempts) + 1
        try:
            completion = await self._create_chat_completion(
                request_kwargs,
                request_timeout_seconds=request_timeout_seconds,
                request_max_retries=request_max_retries,
            )
        except Exception as exc:
            transport = transport_timing(self.transport)
            attempts.append(
                LLMProviderAttempt(
                    attempt=attempt_number,
                    provider=self.provider_capabilities.name,
                    model=model,
                    status="error",
                    latency_ms=round((time.perf_counter() - started_at) * 1000),
                    native_tools="tools" in request_kwargs,
                    error=_summarize_provider_error(exc),
                    request_ms=transport.request_ms,
                )
            )
            try:
                setattr(exc, "nycti_provider_attempts", list(attempts))
            except Exception:
                pass
            raise
        transport = transport_timing(self.transport)
        attempts.append(
            LLMProviderAttempt(
                attempt=attempt_number,
                provider=self.provider_capabilities.name,
                model=model,
                status="ok",
                latency_ms=round((time.perf_counter() - started_at) * 1000),
                native_tools="tools" in request_kwargs,
                request_ms=transport.request_ms,
            )
        )
        return completion

    async def _create_chat_completion(
        self,
        request_kwargs: dict[str, object],
        *,
        request_timeout_seconds: float | None,
        request_max_retries: int | None,
    ):
        if request_timeout_seconds is None and request_max_retries is None:
            request_timeout_seconds = self.provider_capabilities.request_timeout_seconds
            request_max_retries = self.provider_capabilities.request_max_retries
        return await self.transport.create_chat_completion(
            client=self.client,
            request=request_kwargs,
            timeout_seconds=request_timeout_seconds,
            max_retries=request_max_retries,
        )

    async def _create_response(
        self,
        request_kwargs: dict[str, object],
        *,
        request_timeout_seconds: float | None,
        request_max_retries: int | None,
    ):
        if request_timeout_seconds is None and request_max_retries is None:
            request_timeout_seconds = self.provider_capabilities.request_timeout_seconds
            request_max_retries = self.provider_capabilities.request_max_retries
        return await self.transport.create_response(
            client=self.client,
            request=request_kwargs,
            timeout_seconds=request_timeout_seconds,
            max_retries=request_max_retries,
        )

    def _is_chat_model_unhealthy(self, model: str) -> bool:
        unhealthy_until = self._unhealthy_chat_models_until.get(model)
        if unhealthy_until is None:
            return False
        if unhealthy_until <= time.monotonic():
            self._unhealthy_chat_models_until.pop(model, None)
            return False
        return True

    def is_model_available(self, model: str | None) -> bool:
        normalized = str(model or "").strip()
        return bool(normalized) and not self._is_chat_model_unhealthy(normalized)

    def _mark_chat_model_unhealthy(
        self,
        model: str,
        error_kind: ProviderErrorKind = ProviderErrorKind.DEPLOYMENT,
    ) -> None:
        cooldown_seconds = failover_cooldown_seconds(error_kind)
        if cooldown_seconds <= 0:
            return
        self._unhealthy_chat_models_until[model] = time.monotonic() + cooldown_seconds

    def _clear_chat_model_cooldown(self, model: str) -> None:
        self._unhealthy_chat_models_until.pop(model, None)

    def _estimate_cost(self, *, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing = DEFAULT_PRICING.get(model)
        if pricing is None:
            lower = model.lower()
            if "nano" in lower:
                pricing = ModelPricing(0.10, 0.40)
            elif "mini" in lower:
                pricing = ModelPricing(0.40, 1.60)
            else:
                return 0.0

        prompt_cost = (prompt_tokens / 1_000_000) * pricing.input_per_million
        completion_cost = (completion_tokens / 1_000_000) * pricing.output_per_million
        return round(prompt_cost + completion_cost, 8)


def _chat_request_debug_json(request_kwargs: dict[str, object]) -> str:
    return json.dumps(
        _redact_request_debug_value(request_kwargs),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )


def _redact_request_debug_value(value: object) -> object:
    if isinstance(value, list):
        return [_redact_request_debug_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "[encrypted reasoning omitted]"
                if key == "encrypted_content"
                else _redact_request_debug_value(item)
            )
            for key, item in value.items()
        }
    return value


def _attach_debug_request(exc: Exception, request_kwargs: dict[str, object]) -> None:
    try:
        setattr(exc, "nycti_request_json", _chat_request_debug_json(request_kwargs))
    except Exception:
        return


def _without_responses_output_items(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not any(RESPONSES_OUTPUT_ITEMS_KEY in message for message in messages):
        return messages
    return [
        {key: value for key, value in message.items() if key != RESPONSES_OUTPUT_ITEMS_KEY}
        for message in messages
    ]


def _build_chat_completion_request(
    *, model: str, messages: list[dict[str, object]], max_tokens: int,
    temperature: float, capabilities: ProviderCapabilities | None = None,
    reasoning_effort: str = "",
) -> dict[str, object]:
    provider = capabilities or capabilities_for_base_url("https://openai-compatible.invalid/v1")
    effort = _reasoning_effort_for_model(model=model, effort=reasoning_effort)
    token_field = "max_completion_tokens" if provider.name == "openai" and model.startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
    return {
        "model": model,
        "messages": messages,
        token_field: max_tokens,
        **({"reasoning_effort": effort} if effort else {"temperature": temperature}),
    }
