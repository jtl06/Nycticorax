from __future__ import annotations

from nycti.bot import NyctiBot
from nycti.browser import BrowserClient
from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import OpenAIClient
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService
from nycti.member_aliases import MemberAliasService
from nycti.procedures import ProcedureMemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.yahoo import YahooFinanceClient
from nycti.youtube import YouTubeTranscriptClient


def build_nycti_bot(
    *,
    settings: Settings,
    database: Database,
    llm_client: OpenAIClient,
) -> NyctiBot:
    """Build the shared production agent graph used by every execution mode."""

    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
        llm_client=llm_client,
        embedding_model=settings.openai_embedding_model,
    )
    procedure_memory_service = (
        ProcedureMemoryService(settings=settings, llm_client=llm_client)
        if settings.procedural_memory_enabled
        else None
    )
    return NyctiBot(
        settings=settings,
        database=database,
        llm_client=llm_client,
        market_data_client=TwelveDataClient(
            settings.twelve_data_api_key,
            base_url=settings.twelve_data_base_url,
        ),
        yahoo_finance_client=YahooFinanceClient(),
        tavily_client=TavilyClient(
            settings.tavily_api_key,
            search_depth=settings.tavily_search_depth,
        ),
        browser_client=BrowserClient(
            enabled=settings.browser_tool_enabled,
            timeout_seconds=settings.browser_tool_timeout_seconds,
            headless=settings.browser_tool_headless,
            allow_headed=settings.browser_tool_allow_headed,
        ),
        youtube_client=YouTubeTranscriptClient(
            enabled=settings.youtube_transcript_enabled,
            timeout_seconds=settings.youtube_transcript_timeout_seconds,
        ),
        memory_service=memory_service,
        channel_alias_service=ChannelAliasService(),
        member_alias_service=MemberAliasService(),
        reminder_service=ReminderService(),
        procedure_memory_service=procedure_memory_service,
    )
