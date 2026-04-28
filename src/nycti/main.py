from __future__ import annotations

import asyncio
import logging

from nycti.browser import BrowserClient
from nycti.bot import NyctiBot
from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import OpenAIClient
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService
from nycti.member_aliases import MemberAliasService
from nycti.reminders.service import ReminderService
from nycti.rss.client import RSSClient
from nycti.rss.service import RSSService
from nycti.startup import (
    MAX_DISCORD_START_RETRIES,
    compute_discord_start_backoff_seconds,
    is_retryable_discord_start_error,
)
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.yahoo import YahooFinanceClient
from nycti.youtube import YouTubeTranscriptClient

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    settings = Settings.from_env()
    database = Database(settings)
    llm_client = OpenAIClient(settings)
    market_data_client = TwelveDataClient(
        settings.twelve_data_api_key,
        base_url=settings.twelve_data_base_url,
    )
    yahoo_finance_client = YahooFinanceClient()
    tavily_client = TavilyClient(settings.tavily_api_key)
    browser_client = BrowserClient(
        enabled=settings.browser_tool_enabled,
        timeout_seconds=settings.browser_tool_timeout_seconds,
        headless=settings.browser_tool_headless,
        allow_headed=settings.browser_tool_allow_headed,
    )
    youtube_client = YouTubeTranscriptClient(
        enabled=settings.youtube_transcript_enabled,
        timeout_seconds=settings.youtube_transcript_timeout_seconds,
    )
    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
        llm_client=llm_client,
        embedding_model=settings.openai_embedding_model,
    )
    channel_alias_service = ChannelAliasService()
    member_alias_service = MemberAliasService()
    reminder_service = ReminderService()
    rss_service = RSSService(
        client=RSSClient(),
        feed_urls=settings.news_rss_urls,
        default_channel_id=settings.news_channel_id,
        post_limit_per_poll=settings.news_post_limit_per_poll,
    )
    attempt = 1
    while True:
        bot = NyctiBot(
            settings=settings,
            database=database,
            llm_client=llm_client,
            market_data_client=market_data_client,
            yahoo_finance_client=yahoo_finance_client,
            tavily_client=tavily_client,
            browser_client=browser_client,
            youtube_client=youtube_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            member_alias_service=member_alias_service,
            reminder_service=reminder_service,
            rss_service=rss_service,
        )
        try:
            async with bot:
                await bot.start(settings.discord_token)
            return
        except Exception as exc:
            if not is_retryable_discord_start_error(exc) or attempt >= MAX_DISCORD_START_RETRIES:
                raise
            backoff_seconds = compute_discord_start_backoff_seconds(attempt)
            LOGGER.warning(
                "Discord startup hit a temporary rate limit or edge block (attempt %s/%s). Retrying in %s seconds.",
                attempt,
                MAX_DISCORD_START_RETRIES,
                backoff_seconds,
            )
            attempt += 1
            await asyncio.sleep(backoff_seconds)


def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
