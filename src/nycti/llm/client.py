from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time

from openai import AsyncOpenAI

from nycti.config import Settings
from nycti.llm.provider_policy import (
    ProviderCapabilities,
    ProviderErrorKind,
    capabilities_for_base_url,
    classify_provider_error,
    failover_cooldown_seconds,
)
from nycti.llm.tool_calls import (
    LLMToolCall,
    _extract_inline_tool_calls,
    _strip_inline_tool_call_markup,
)

LOGGER = logging.getLogger(__name__)
@dataclass(slots=True)
class LLMUsage:
    feature: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    provider: str = "openai-default"
    requested_model: str = ""
    attempt: int = 1


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: LLMUsage


@dataclass(slots=True)
class EmbeddingResult:
    embedding: list[float]
    usage: LLMUsage


@dataclass(slots=True)
class LLMChatTurn:
    text: str
    raw_text: str
    usage: LLMUsage
    tool_calls: list[LLMToolCall]
    reasoning_content: str
    finish_reason: str
    native_tool_calling_failed: bool = False
    native_tool_failure_request_json: str = ""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float


DEFAULT_PRICING: dict[str, ModelPricing] = {
    "gpt-4.1-mini": ModelPricing(input_per_million=0.40, output_per_million=1.60),
    "gpt-4.1-nano": ModelPricing(input_per_million=0.10, output_per_million=0.40),
    "text-embedding-3-small": ModelPricing(input_per_million=0.02, output_per_million=0.0),
    "text-embedding-3-large": ModelPricing(input_per_million=0.13, output_per_million=0.0),
}

EFFICIENCY_FEATURES = frozenset(
    {
        "extended_context_summary",
        "memory_extract",
        "personal_profile_update",
        "youtube_transcript_summary",
    }
)


class OpenAIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        client_kwargs = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        self.client = AsyncOpenAI(**client_kwargs)
        embedding_client_kwargs = {"api_key": settings.openai_embedding_api_key or settings.openai_api_key}
        if settings.openai_embedding_base_url:
            embedding_client_kwargs["base_url"] = settings.openai_embedding_base_url
        elif settings.openai_embedding_api_key is None and settings.openai_base_url:
            embedding_client_kwargs["base_url"] = settings.openai_base_url
        self.embedding_client = AsyncOpenAI(**embedding_client_kwargs)
        self.provider_capabilities = capabilities_for_base_url(settings.openai_base_url)
        self._unhealthy_chat_models_until: dict[str, float] = {}

    async def complete_chat(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        request_timeout_seconds: float | None = None,
        request_max_retries: int | None = None,
    ) -> LLMResult:
        result = await self.complete_chat_turn(
            model=model,
            feature=feature,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
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
        request_timeout_seconds: float | None = None,
        request_max_retries: int | None = None,
    ) -> LLMChatTurn:
        completion = None
        actual_model = model
        last_error: Exception | None = None
        candidate_models = self._chat_model_candidates(model)
        native_tool_calling_failed = False
        native_tool_failure_request_json = ""
        attempt_number = 0
        native_tools_requested = bool(tools and use_native_tools)
        native_tools_allowed = native_tools_requested and self.provider_capabilities.native_tools
        native_tool_calling_failed = native_tools_requested and not native_tools_allowed
        request_messages = _plain_chat_retry_messages(messages) if tools and not native_tools_allowed else messages
        if not candidate_models:
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
                request_variants = _build_chat_completion_request_variants(
                    model=candidate_model,
                    messages=request_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    capabilities=self.provider_capabilities,
                    extra_body=_efficiency_model_extra_body(
                        feature=feature,
                        candidate_model=candidate_model,
                        configured_model=str(
                            getattr(self.settings, "openai_memory_model", "") or ""
                        ),
                    ),
                )
                for index, request_kwargs in enumerate(request_variants):
                    if native_tools_allowed:
                        request_kwargs["tools"] = tools
                    try:
                        attempt_number += 1
                        LOGGER.info(
                            "Chat completion attempt feature=%s provider=%s model=%s candidate=%s/%s variant=%s token_field=%s native_tools=%s tool_count=%s.",
                            feature,
                            _provider_label(self.settings.openai_base_url),
                            candidate_model,
                            candidate_index + 1,
                            len(candidate_models),
                            index + 1,
                            _request_token_field(request_kwargs),
                            "yes" if "tools" in request_kwargs else "no",
                            len(tools or []),
                        )
                        completion = await self._create_chat_completion(
                            request_kwargs,
                            request_timeout_seconds=request_timeout_seconds,
                            request_max_retries=request_max_retries,
                        )
                        actual_model = candidate_model
                        self._clear_chat_model_cooldown(candidate_model)
                        LOGGER.info(
                            "Chat completion success feature=%s model=%s candidate=%s/%s variant=%s native_tools=%s.",
                            feature,
                            candidate_model,
                            candidate_index + 1,
                            len(candidate_models),
                            index + 1,
                            "yes" if "tools" in request_kwargs else "no",
                        )
                        break
                    except Exception as exc:
                        _attach_debug_request(exc, request_kwargs)
                        LOGGER.warning(
                            "Chat completion failed feature=%s model=%s candidate=%s/%s variant=%s native_tools=%s error=%s.",
                            feature,
                            candidate_model,
                            candidate_index + 1,
                            len(candidate_models),
                            index + 1,
                            "yes" if "tools" in request_kwargs else "no",
                            _summarize_provider_error(exc),
                        )
                        if tools and _should_retry_without_native_tools(exc):
                            native_tool_failure_request_json = _chat_request_debug_json(request_kwargs)
                            stripped_kwargs = dict(request_kwargs)
                            stripped_kwargs.pop("tools", None)
                            stripped_messages = _plain_chat_retry_messages(messages)
                            stripped_kwargs["messages"] = stripped_messages
                            LOGGER.warning(
                                "Chat model %s rejected native tool schemas; retrying once without native tools feature=%s token_field=%s original_messages=%s stripped_messages=%s original_error=%s.",
                                candidate_model,
                                feature,
                                _request_token_field(stripped_kwargs),
                                len(messages),
                                len(stripped_messages),
                                _summarize_provider_error(exc),
                            )
                            try:
                                attempt_number += 1
                                completion = await self._create_chat_completion(
                                    stripped_kwargs,
                                    request_timeout_seconds=request_timeout_seconds,
                                    request_max_retries=request_max_retries,
                                )
                            except Exception as stripped_exc:
                                _attach_debug_request(stripped_exc, stripped_kwargs)
                                if _should_compact_plain_retry(stripped_exc):
                                    compact_kwargs = dict(stripped_kwargs)
                                    compact_messages = _compact_plain_retry_messages(stripped_messages)
                                    compact_kwargs["messages"] = compact_messages
                                    LOGGER.warning(
                                        "Chat completion no-native-tools retry failed; retrying compact plain chat feature=%s model=%s candidate=%s/%s token_field=%s stripped_messages=%s compact_messages=%s compact_chars=%s error=%s.",
                                        feature,
                                        candidate_model,
                                        candidate_index + 1,
                                        len(candidate_models),
                                        _request_token_field(compact_kwargs),
                                        len(stripped_messages),
                                        len(compact_messages),
                                        _message_content_chars(compact_messages),
                                        _summarize_provider_error(stripped_exc),
                                    )
                                    try:
                                        attempt_number += 1
                                        completion = await self._create_chat_completion(
                                            compact_kwargs,
                                            request_timeout_seconds=request_timeout_seconds,
                                            request_max_retries=request_max_retries,
                                        )
                                    except Exception as compact_exc:
                                        _attach_debug_request(compact_exc, compact_kwargs)
                                        LOGGER.warning(
                                            "Chat completion compact plain retry failed feature=%s model=%s candidate=%s/%s token_field=%s error=%s.",
                                            feature,
                                            candidate_model,
                                            candidate_index + 1,
                                            len(candidate_models),
                                            _request_token_field(compact_kwargs),
                                            _summarize_provider_error(compact_exc),
                                        )
                                        raise compact_exc from stripped_exc
                                    actual_model = candidate_model
                                    native_tool_calling_failed = True
                                    self._clear_chat_model_cooldown(candidate_model)
                                    LOGGER.info(
                                        "Chat completion compact plain retry succeeded feature=%s model=%s candidate=%s/%s.",
                                        feature,
                                        candidate_model,
                                        candidate_index + 1,
                                        len(candidate_models),
                                    )
                                    break
                                LOGGER.warning(
                                    "Chat completion no-native-tools retry failed feature=%s model=%s candidate=%s/%s token_field=%s error=%s.",
                                    feature,
                                    candidate_model,
                                    candidate_index + 1,
                                    len(candidate_models),
                                    _request_token_field(stripped_kwargs),
                                    _summarize_provider_error(stripped_exc),
                                )
                                raise stripped_exc from exc
                            actual_model = candidate_model
                            native_tool_calling_failed = True
                            self._clear_chat_model_cooldown(candidate_model)
                            LOGGER.info(
                                "Chat completion no-native-tools retry succeeded feature=%s model=%s candidate=%s/%s.",
                                feature,
                                candidate_model,
                                candidate_index + 1,
                                len(candidate_models),
                            )
                            break
                        if index + 1 < len(request_variants) and _is_token_field_conflict_error(exc):
                            continue
                        raise
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
                raise
            if completion is not None:
                break
        if completion is None:
            assert last_error is not None
            raise last_error
        assert completion is not None
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
        if not tool_calls and tools:
            content, tool_calls = _extract_inline_tool_calls(content, tools)
        else:
            content = _strip_inline_tool_call_markup(content)
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else prompt_tokens + completion_tokens
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
            native_tool_calling_failed=native_tool_calling_failed,
            native_tool_failure_request_json=native_tool_failure_request_json,
        )

    def _chat_model_candidates(self, model: str) -> list[str]:
        candidates = [model]
        if model == self.settings.openai_chat_model:
            candidates.extend(self.settings.openai_chat_model_fallbacks)
            efficiency_model = str(getattr(self.settings, "openai_memory_model", "") or "").strip()
            if efficiency_model and efficiency_model not in candidates:
                candidates.append(efficiency_model)
        unique_candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
        return [candidate for candidate in unique_candidates if not self._is_chat_model_unhealthy(candidate)]

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
        if not hasattr(self.client, "with_options"):
            return await self.client.chat.completions.create(**request_kwargs)
        option_kwargs: dict[str, object] = {}
        if request_timeout_seconds is not None:
            option_kwargs["timeout"] = request_timeout_seconds
        if request_max_retries is not None:
            option_kwargs["max_retries"] = request_max_retries
        client = self.client.with_options(**option_kwargs)
        return await client.chat.completions.create(**request_kwargs)

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


def _should_fail_over_chat_model(exc: Exception) -> bool:
    return classify_provider_error(exc) in {
        ProviderErrorKind.DEPLOYMENT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.ACCESS_DENIED,
        ProviderErrorKind.TRANSIENT,
    }


def _is_deterministic_model_unavailable_error(exc: Exception) -> bool:
    return classify_provider_error(exc) == ProviderErrorKind.DEPLOYMENT


def _should_retry_without_native_tools(exc: Exception) -> bool:
    return classify_provider_error(exc) == ProviderErrorKind.TOOL_INCOMPATIBLE


def _should_compact_plain_retry(exc: Exception) -> bool:
    return _should_retry_without_native_tools(exc) or _should_fail_over_chat_model(exc)


def is_transient_provider_error(exc: Exception) -> bool:
    return classify_provider_error(exc) in {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TRANSIENT,
    }


def _provider_label(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return "openai-default"
    return normalized.rstrip("/")


def _request_token_field(request_kwargs: dict[str, object]) -> str:
    if "max_tokens" in request_kwargs:
        return "max_tokens"
    if "max_completion_tokens" in request_kwargs:
        return "max_completion_tokens"
    return "(none)"


def _summarize_provider_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


def _compact_plain_retry_messages(messages: list[dict[str, object]], *, max_context_chars: int = 2800) -> list[dict[str, object]]:
    user_text = _last_text_message(messages)
    current_request = _extract_prompt_section(user_text, "Current request:", "Recent channel context:")
    recent_context = _extract_prompt_section(user_text, "Recent channel context:", "Extended channel context:")
    current_datetime = _extract_prompt_section(user_text, "Current local date/time:", "Current request:")
    if not current_request:
        current_request = _truncate_text(user_text, max_context_chars)
    compact_user_parts = []
    if current_datetime:
        compact_user_parts.append(f"Current date/time:\n{_truncate_text(current_datetime, 300)}")
    compact_user_parts.append(f"Current request:\n{_truncate_text(current_request, 1200)}")
    if recent_context and recent_context != "(no recent context)":
        compact_user_parts.append(f"Recent context:\n{_truncate_text(recent_context, max_context_chars)}")
    compact_user_parts.append("Answer concisely from this compact fallback context. Do not call tools.")
    return [
        {
            "role": "system",
            "content": "You are Nycti, a concise Discord assistant. Answer directly and naturally.",
        },
        {
            "role": "user",
            "content": "\n\n".join(compact_user_parts),
        },
    ]


def _last_text_message(messages: list[dict[str, object]]) -> str:
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _extract_prompt_section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _truncate_text(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def _message_content_chars(messages: list[dict[str, object]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def _chat_request_debug_json(request_kwargs: dict[str, object]) -> str:
    return json.dumps(request_kwargs, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _attach_debug_request(exc: Exception, request_kwargs: dict[str, object]) -> None:
    try:
        setattr(exc, "nycti_request_json", _chat_request_debug_json(request_kwargs))
    except Exception:
        return


def _plain_chat_retry_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    plain_messages: list[dict[str, object]] = []
    for message in _strip_tool_guidance_messages(messages):
        role = message.get("role")
        content = message.get("content")
        if role == "tool":
            if not isinstance(content, str) or not content.strip():
                continue
            name = str(message.get("name") or "tool").strip() or "tool"
            plain_messages.append(
                {
                    "role": "user",
                    "content": f"Tool result from {name}:\n{content.strip()}",
                }
            )
            continue
        if "tool_calls" in message:
            if isinstance(content, str) and content.strip():
                plain_messages.append(
                    {
                        "role": role if isinstance(role, str) else "assistant",
                        "content": content,
                    }
                )
            continue
        plain_messages.append(message)
    return plain_messages or messages


def _strip_tool_guidance_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    stripped = [
        message
        for message in messages
        if not _is_tool_guidance_message(message)
    ]
    return stripped or messages


def _is_tool_guidance_message(message: dict[str, object]) -> bool:
    content = message.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.lstrip()
    return stripped.startswith(("Available tools this turn:", "Tool-loop discipline:"))


def _build_chat_completion_request_variants(
    *,
    model: str,
    messages: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
    capabilities: ProviderCapabilities | None = None,
    extra_body: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    provider = capabilities or capabilities_for_base_url("https://openai-compatible.invalid/v1")
    return [
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            **({token_field: max_tokens} if token_field else {}),
            **({"extra_body": extra_body} if extra_body else {}),
        }
        for token_field in provider.token_fields(has_images=_messages_include_image_content(messages))
    ]


def _efficiency_model_extra_body(
    *,
    feature: str,
    candidate_model: str,
    configured_model: str,
) -> dict[str, object] | None:
    if feature not in EFFICIENCY_FEATURES or candidate_model != configured_model:
        return None
    normalized_model = candidate_model.casefold().replace("_", "-")
    if "kimi-k2-5" not in normalized_model and "kimi-k2.5" not in normalized_model:
        return None
    return {"chat_template_kwargs": {"thinking": False}}


def _messages_include_image_content(messages: list[dict[str, object]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                return True
    return False


def _is_token_field_conflict_error(exc: Exception) -> bool:
    return "max_tokens and max_completion_tokens cannot both be set" in str(exc).casefold()
