from __future__ import annotations

import asyncio
import logging

from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import OpenAIClient
from nycti.llm.token_quota import DailyTokenQuota
from nycti.runtime import build_nycti_bot
from nycti.startup import (
    MAX_DISCORD_START_RETRIES,
    compute_discord_start_backoff_seconds,
    is_retryable_discord_start_error,
)

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    settings = Settings.from_env()
    database = Database(settings)
    token_quota = (
        DailyTokenQuota(
            database,
            budgets=dict(settings.openai_daily_token_budgets),
            fallback_model=settings.openai_daily_token_fallback_model or "",
            fallback_reasoning_effort=(
                settings.openai_daily_token_fallback_reasoning_effort or "high"
            ),
        )
        if settings.openai_daily_token_budgets
        else None
    )
    llm_client = OpenAIClient(settings, token_quota=token_quota)
    attempt = 1
    while True:
        bot = build_nycti_bot(
            settings=settings,
            database=database,
            llm_client=llm_client,
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
