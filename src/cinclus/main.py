from __future__ import annotations

import asyncio
import logging

from cinclus.bot import CinclusBot
from cinclus.config import Settings
from cinclus.db.session import Database
from cinclus.llm.client import OpenAIClient
from cinclus.memory.extractor import MemoryExtractor
from cinclus.memory.retriever import MemoryRetriever
from cinclus.memory.service import MemoryService


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    settings = Settings.from_env()
    database = Database(settings)
    llm_client = OpenAIClient(settings)
    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
    )
    bot = CinclusBot(
        settings=settings,
        database=database,
        llm_client=llm_client,
        memory_service=memory_service,
    )
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
