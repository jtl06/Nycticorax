from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time

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
                        completion = await self.client.chat.completions.create(**request_kwargs)
                        actual_model = candidate_model
                        self._clear_chat_model_cooldown(candidate_model)
                        break
                    except Exception as exc:
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
                        "Chat model %s failed with a model-level provider error; falling back to %s.",
                        candidate_model,
                        candidate_models[candidate_index + 1],
                    )
                    continue
                raise
            if completion is not None:
                break
        if completion is None:
            assert last_error is not None
            raise last_error
        assert completion is not None
        message = completion.choices[0].message
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
        )

    def _chat_model_candidates(self, model: str) -> list[str]:
        candidates = [model]
        if model == self.settings.openai_chat_model:
            candidates.extend(self.settings.openai_chat_model_fallbacks)
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


def _extract_inline_tool_calls(
    text: str,
    tools: list[dict[str, object]],
) -> tuple[str, list[LLMToolCall]]:
    match = INLINE_TOOL_SECTION_PATTERN.search(text)
    if match is None:
        return text, []

    available_names = _available_tool_names(tools)
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
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", header):
        if token in available_names:
            return token

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
        "model prediction failed",
    )
    return any(signal in normalized for signal in signals)
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
