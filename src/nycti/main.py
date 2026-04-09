from __future__ import annotations

import asyncio
import logging

from nycti.bot import NyctiBot
from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import OpenAIClient
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.startup import (
    MAX_DISCORD_START_RETRIES,
    compute_discord_start_backoff_seconds,
    is_retryable_discord_start_error,
)
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient

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
    tavily_client = TavilyClient(settings.tavily_api_key)
    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
        llm_client=llm_client,
        embedding_model=settings.openai_embedding_model,
    )
    channel_alias_service = ChannelAliasService()
    reminder_service = ReminderService()
    attempt = 1
    while True:
        bot = NyctiBot(
            settings=settings,
            database=database,
            llm_client=llm_client,
            market_data_client=market_data_client,
            tavily_client=tavily_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            reminder_service=reminder_service,
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
