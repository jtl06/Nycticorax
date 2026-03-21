from __future__ import annotations

import asyncio
import logging

from nycti.bot import NyctiBot
from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import OpenAIClient
from nycti.tavily.client import TavilyClient
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    settings = Settings.from_env()
    database = Database(settings)
    llm_client = OpenAIClient(settings)
    tavily_client = TavilyClient(settings.tavily_api_key)
    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
        llm_client=llm_client,
        embedding_model=settings.openai_embedding_model,
    )
    channel_alias_service = ChannelAliasService()
    reminder_service = ReminderService()
    bot = NyctiBot(
        settings=settings,
        database=database,
        llm_client=llm_client,
        tavily_client=tavily_client,
        memory_service=memory_service,
        channel_alias_service=channel_alias_service,
        reminder_service=reminder_service,
    )
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
