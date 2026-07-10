from __future__ import annotations

from dataclasses import dataclass, field

from nycti.llm.tool_calls import LLMToolCall


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
    cached_prompt_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: LLMUsage


@dataclass(slots=True)
class EmbeddingResult:
    embedding: list[float]
    usage: LLMUsage


@dataclass(frozen=True, slots=True)
class LLMProviderAttempt:
    attempt: int
    provider: str
    model: str
    status: str
    latency_ms: int
    native_tools: bool
    error: str = ""


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
    provider_attempts: list[LLMProviderAttempt] = field(default_factory=list)
    refusal: str = ""
    incomplete_details: dict[str, object] = field(default_factory=dict)
    response_output_items: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float


DEFAULT_PRICING: dict[str, ModelPricing] = {
    "gpt-5.6-luna": ModelPricing(input_per_million=1.00, output_per_million=6.00),
    "deepseek-ai/DeepSeek-V4-Pro": ModelPricing(
        input_per_million=1.30,
        output_per_million=2.60,
    ),
    "gpt-4.1-mini": ModelPricing(input_per_million=0.40, output_per_million=1.60),
    "gpt-4.1-nano": ModelPricing(input_per_million=0.10, output_per_million=0.40),
    "text-embedding-3-small": ModelPricing(input_per_million=0.02, output_per_million=0.0),
    "text-embedding-3-large": ModelPricing(input_per_million=0.13, output_per_million=0.0),
}
