from __future__ import annotations

from typing import TYPE_CHECKING

from nycti.chat.deep_research import CompositeDeepResearchService, DeepResearchConfig

if TYPE_CHECKING:
    from nycti.config import Settings
    from nycti.llm.client import OpenAIClient
    from nycti.tavily.client import TavilyClient


def build_composite_deep_research_service(
    settings: Settings,
    llm_client: OpenAIClient,
    tavily_client: TavilyClient,
) -> CompositeDeepResearchService | None:
    """Build the callable research tool using the cheapest configured planning client."""
    if not getattr(tavily_client, "api_key", None):
        return None
    research_client = llm_client
    research_model = settings.openai_memory_model
    if llm_client.fallback_client is not None:
        research_client = llm_client.fallback_client
        research_model = research_client.settings.openai_chat_model
    return CompositeDeepResearchService(
        llm_client=research_client,
        tavily_client=tavily_client,
        config=DeepResearchConfig(economy_model=research_model),
    )
