from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import tempfile
import time
from typing import Mapping

from nycti.bot import NyctiBot
from nycti.bot_support import BENCHMARK_USER_ID, BENCHMARK_USER_NAME
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
from nycti.timing import elapsed_ms
from nycti.twelvedata.client import TwelveDataClient
from nycti.usage import build_additive_timing_metrics
from nycti.yahoo import YahooFinanceClient
from nycti.youtube import YouTubeTranscriptClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask the deployed Nycti agent directly without posting to Discord."
    )
    parser.add_argument("prompt", help="One user prompt to run through the foreground agent.")
    parser.add_argument(
        "--depth",
        choices=("auto", "quick", "grounded", "deep"),
        default="auto",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="Optional synthetic Discord context line; repeat for multiple lines.",
    )
    args = parser.parse_args()
    asyncio.run(
        run_smoke(
            prompt=args.prompt,
            depth=args.depth,
            context_lines=list(args.context),
        )
    )


async def run_smoke(
    *,
    prompt: str,
    depth: str = "auto",
    context_lines: list[str] | None = None,
) -> None:
    cleaned_prompt = " ".join(prompt.split()).strip()
    if not cleaned_prompt:
        raise ValueError("prompt must not be blank")
    if len(cleaned_prompt) > 2_000:
        raise ValueError("prompt must not exceed 2000 characters")

    with tempfile.TemporaryDirectory(prefix="nycti-smoke-") as temp_dir:
        settings = replace(
            _load_settings_with_database(
                f"sqlite+aiosqlite:///{temp_dir}/smoke.db"
            ),
            database_url=f"sqlite+aiosqlite:///{temp_dir}/smoke.db",
            error_debug_channel_id=None,
            persist_bad_bot_diagnostics=False,
            openai_daily_token_budgets=(),
            openai_daily_token_fallback_model=None,
            openai_daily_token_fallback_reasoning_effort=None,
        )
        database = Database(settings)
        await database.init_models()
        llm_client = OpenAIClient(settings)
        bot = _build_bot(settings, database, llm_client)
        started_at = time.perf_counter()
        try:
            answer, raw_metrics = await bot._generate_reply(
                guild_id=None,
                channel_id=None,
                user_id=BENCHMARK_USER_ID,
                user_name=BENCHMARK_USER_NAME,
                user_global_name=BENCHMARK_USER_NAME,
                mentioned_user_ids=[],
                prompt=cleaned_prompt,
                context_lines=list(context_lines or ()),
                image_attachment_urls=[],
                image_context_lines=[],
                source_message_id=None,
                request_started_at=started_at,
                depth_override=None if depth == "auto" else depth,
                collect_latency_debug=True,
                include_memories=False,
            )
            metrics = dict(raw_metrics or {})
            metrics["context_fetch_ms"] = 0
            metrics["reply_send_ms"] = 0
            metrics["end_to_end_ms"] = elapsed_ms(started_at)
            print(
                json.dumps(
                    build_smoke_result(
                        prompt=cleaned_prompt,
                        answer=answer,
                        depth=depth,
                        metrics=metrics,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
        finally:
            await bot.close()
            await database.engine.dispose()
            await _close_llm_client(llm_client)


def build_smoke_result(
    *,
    prompt: str,
    answer: str,
    depth: str,
    metrics: Mapping[str, int | str],
) -> dict[str, object]:
    raw_steps = metrics.get("_diagnostic_agent_steps_json", "[]")
    try:
        steps = json.loads(raw_steps) if isinstance(raw_steps, str) else raw_steps
    except (json.JSONDecodeError, TypeError):
        steps = []
    public_metrics = {
        key: value
        for key, value in metrics.items()
        if not key.startswith("_")
    }
    return {
        "mode": "railway_headless_smoke",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "depth": depth,
        "answer": answer,
        "metrics": public_metrics,
        "phases": build_additive_timing_metrics(public_metrics),
        "steps": steps,
    }


def _build_bot(
    settings: Settings,
    database: Database,
    llm_client: OpenAIClient,
) -> NyctiBot:
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


def _load_settings_with_database(database_url: str) -> Settings:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        return Settings.from_env()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _close_llm_client(client: OpenAIClient) -> None:
    await client.client.close()
    if client.embedding_client is not client.client:
        await client.embedding_client.close()
    if client.fallback_client is not None:
        await _close_llm_client(client.fallback_client)


if __name__ == "__main__":
    main()
