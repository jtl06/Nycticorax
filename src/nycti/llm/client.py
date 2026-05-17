from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time
import xml.etree.ElementTree as ElementTree

from openai import AsyncOpenAI

from nycti.config import Settings

LOGGER = logging.getLogger(__name__)
MODEL_FAILOVER_COOLDOWN_SECONDS = 900


@dataclass(slots=True)
class LLMUsage:
    feature: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: LLMUsage


@dataclass(slots=True)
class EmbeddingResult:
    embedding: list[float]
    usage: LLMUsage


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class LLMChatTurn:
    text: str
    raw_text: str
    usage: LLMUsage
    tool_calls: list[LLMToolCall]
    reasoning_content: str
    finish_reason: str


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
        self._unhealthy_chat_models_until: dict[str, float] = {}

    async def complete_chat(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        result = await self.complete_chat_turn(
            model=model,
            feature=feature,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
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
    ) -> LLMChatTurn:
        completion = None
        actual_model = model
        last_error: Exception | None = None
        candidate_models = self._chat_model_candidates(model)
        LOGGER.info(
            "Chat completion start feature=%s provider=%s requested_model=%s candidates=%s native_tools=%s tool_count=%s message_count=%s.",
            feature,
            _provider_label(self.settings.openai_base_url),
            model,
            " -> ".join(candidate_models),
            "yes" if tools else "no",
            len(tools or []),
            len(messages),
        )
        for candidate_index, candidate_model in enumerate(candidate_models):
            try:
                request_variants = _build_chat_completion_request_variants(
                    model=candidate_model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                for index, request_kwargs in enumerate(request_variants):
                    if tools:
                        request_kwargs["tools"] = tools
                    try:
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
                        completion = await self.client.chat.completions.create(**request_kwargs)
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
                            stripped_kwargs = dict(request_kwargs)
                            stripped_kwargs.pop("tools", None)
                            stripped_messages = _strip_tool_guidance_messages(messages)
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
                                completion = await self.client.chat.completions.create(**stripped_kwargs)
                            except Exception as stripped_exc:
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
                                        completion = await self.client.chat.completions.create(**compact_kwargs)
                                    except Exception as compact_exc:
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
                if (
                    candidate_index + 1 < len(candidate_models)
                    and _should_fail_over_chat_model(exc)
                ):
                    self._mark_chat_model_unhealthy(candidate_model)
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
            ),
            tool_calls=tool_calls,
            reasoning_content=reasoning_content.strip() if reasoning_content else "",
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        )

    def _chat_model_candidates(self, model: str) -> list[str]:
        candidates = [model]
        if model == self.settings.openai_chat_model:
            candidates.extend(self.settings.openai_chat_model_fallbacks)
            efficiency_model = str(getattr(self.settings, "openai_memory_model", "") or "").strip()
            if efficiency_model and efficiency_model not in candidates:
                candidates.append(efficiency_model)
        healthy_candidates = [candidate for candidate in candidates if not self._is_chat_model_unhealthy(candidate)]
        return healthy_candidates or candidates

    def _is_chat_model_unhealthy(self, model: str) -> bool:
        unhealthy_until = self._unhealthy_chat_models_until.get(model)
        if unhealthy_until is None:
            return False
        if unhealthy_until <= time.monotonic():
            self._unhealthy_chat_models_until.pop(model, None)
            return False
        return True

    def _mark_chat_model_unhealthy(self, model: str) -> None:
        self._unhealthy_chat_models_until[model] = time.monotonic() + MODEL_FAILOVER_COOLDOWN_SECONDS

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


INLINE_TOOL_SECTION_PATTERN = re.compile(
    r"<\|tool_calls_section_begin\|>(?P<body>.*?)<\|tool_calls_section_end\|>",
    flags=re.DOTALL,
)
INLINE_TOOL_CALL_PATTERN = re.compile(
    r"<\|tool_call_begin\|>\s*(?P<header>.*?)\s*<\|tool_call_argument_begin\|>\s*(?P<arguments>.*?)\s*<\|tool_call_end\|>",
    flags=re.DOTALL,
)
XML_TOOL_SECTION_PATTERN = re.compile(
    r"<function_calls>\s*(?P<body>.*?)\s*</function_calls>",
    flags=re.DOTALL,
)


def _extract_inline_tool_calls(
    text: str,
    tools: list[dict[str, object]],
) -> tuple[str, list[LLMToolCall]]:
    available_names = _available_tool_names(tools)
    cleaned_text, calls = _extract_special_token_tool_calls(text, available_names)
    if calls:
        return cleaned_text, calls
    if cleaned_text != text:
        return cleaned_text, []
    cleaned_text, calls = _extract_xml_tool_calls(text, available_names)
    if calls:
        return cleaned_text, calls
    if cleaned_text != text:
        return cleaned_text, []
    return _strip_inline_tool_call_markup(text), []


def _extract_special_token_tool_calls(
    text: str,
    available_names: set[str],
) -> tuple[str, list[LLMToolCall]]:
    match = INLINE_TOOL_SECTION_PATTERN.search(text)
    if match is None:
        return text, []

    calls: list[LLMToolCall] = []
    for index, call_match in enumerate(INLINE_TOOL_CALL_PATTERN.finditer(match.group("body")), start=1):
        header = " ".join(call_match.group("header").split())
        arguments = call_match.group("arguments").strip()
        tool_name = _extract_inline_tool_name(header, arguments, available_names)
        if not tool_name:
            continue
        call_id = _extract_inline_tool_id(header, fallback_index=index)
        calls.append(
            LLMToolCall(
                id=call_id,
                name=tool_name,
                arguments=arguments,
            )
        )

    cleaned_text = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned_text, calls


def _extract_xml_tool_calls(
    text: str,
    available_names: set[str],
) -> tuple[str, list[LLMToolCall]]:
    match = XML_TOOL_SECTION_PATTERN.search(text)
    if match is None:
        return text, []
    section = match.group(0)
    try:
        root = ElementTree.fromstring(section)
    except ElementTree.ParseError:
        return text, []
    calls: list[LLMToolCall] = []
    for index, invoke in enumerate(root.findall("invoke"), start=1):
        tool_name = str(invoke.attrib.get("name", "")).strip()
        if tool_name not in available_names:
            continue
        parameters: dict[str, str] = {}
        for parameter in invoke.findall("parameter"):
            name = str(parameter.attrib.get("name", "")).strip()
            if not name:
                continue
            parameters[name] = "".join(parameter.itertext()).strip()
        calls.append(
            LLMToolCall(
                id=f"call_xml_{index}",
                name=tool_name,
                arguments=json.dumps(parameters, separators=(",", ":")),
            )
        )
    if not calls:
        return (text[: match.start()] + text[match.end() :]).strip(), []
    cleaned_text = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned_text, calls


def _strip_inline_tool_call_markup(text: str) -> str:
    cleaned = INLINE_TOOL_SECTION_PATTERN.sub("", text)
    cleaned = XML_TOOL_SECTION_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"<\|tool_calls_section_begin\|>.*\Z", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<function_calls>.*\Z", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _available_tool_names(tools: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _extract_inline_tool_name(header: str, arguments: str, available_names: set[str]) -> str | None:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", header)
    for token in tokens:
        if token in available_names:
            return token

    explicit_name_tokens = [
        token
        for token in tokens
        if token not in {"functions", "function"}
        and not token.startswith("call_")
    ]
    if explicit_name_tokens:
        return None

    if len(available_names) == 1:
        return next(iter(available_names))
    return None


def _extract_inline_tool_id(header: str, fallback_index: int) -> str:
    match = re.search(r"\b(call_[A-Za-z0-9_-]+|call_\d+)\b", header)
    if match is not None:
        return match.group(1)
    return f"call_{fallback_index}"


def _should_fail_over_chat_model(exc: Exception) -> bool:
    normalized = str(exc).casefold()
    signals = (
        "invalid model",
        "unknown model",
        "unsupported model",
        "model not found",
        "no such model",
        "does not exist",
        "error code: 404",
        "status code: 404",
        "error code: 403",
        "status code: 403",
        "403 forbidden",
        "permissiondeniederror",
        "permission denied",
        "access denied",
        "model prediction failed",
        "restricted to shared compute",
        "dedicated nodepool",
        "connection error",
        "internal error",
    )
    return any(signal in normalized for signal in signals)


def _should_retry_without_native_tools(exc: Exception) -> bool:
    normalized = str(exc).casefold()
    signals = (
        "error code: 403",
        "status code: 403",
        "403 forbidden",
        "permissiondeniederror",
        "permission denied",
        "access denied",
        "unsupported tool",
        "tools are not supported",
        "tool use is not supported",
    )
    return any(signal in normalized for signal in signals)


def _should_compact_plain_retry(exc: Exception) -> bool:
    return _should_retry_without_native_tools(exc) or _should_fail_over_chat_model(exc)


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


def _build_chat_completion_request(
    *,
    model: str,
    messages: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
) -> dict[str, object]:
    request_kwargs: dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    # Some OpenAI-compatible providers reject image-bearing requests when `max_tokens`
    # is sent and internally map them to `max_completion_tokens`.
    if _messages_include_image_content(messages):
        request_kwargs["max_completion_tokens"] = max_tokens
    else:
        request_kwargs["max_tokens"] = max_tokens
    return request_kwargs


def _build_chat_completion_request_variants(
    *,
    model: str,
    messages: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
) -> list[dict[str, object]]:
    primary = _build_chat_completion_request(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if not _messages_include_image_content(messages):
        return [primary]

    alternate = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    no_limit = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    return [primary, alternate, no_limit]


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
