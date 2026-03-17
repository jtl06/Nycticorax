from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI

from nycti.config import Settings


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


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float


DEFAULT_PRICING: dict[str, ModelPricing] = {
    "gpt-4.1-mini": ModelPricing(input_per_million=0.40, output_per_million=1.60),
    "gpt-4.1-nano": ModelPricing(input_per_million=0.10, output_per_million=0.40),
}


class OpenAIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def complete_chat(
        self,
        *,
        model: str,
        feature: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        completion = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        content = completion.choices[0].message.content or ""
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else prompt_tokens + completion_tokens
        return LLMResult(
            text=content.strip(),
            usage=LLMUsage(
                feature=feature,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=self._estimate_cost(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                ),
            ),
        )

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
