from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import time

from nycti.bot import NyctiBot
from nycti.bot_support import BENCHMARK_USER_ID, build_isolated_benchmark_context
from nycti.browser import BrowserClient
from nycti.channel_aliases import ChannelAliasService
from nycti.config import Settings
from nycti.db.session import Database
from nycti.discord.live_benchmarks import format_live_benchmark_batch_report
from nycti.live_benchmarks import (
    LIVE_BENCHMARK_FIXTURE_NOW,
    LiveBenchmarkCase,
    LiveBenchmarkExecution,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    build_live_benchmark_fixture_tool_runner,
    load_live_benchmark_image_data_uri,
    load_live_benchmark_manifest,
    run_live_benchmark_suite,
)
from nycti.llm.client import OpenAIClient
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService
from nycti.member_aliases import MemberAliasService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.yahoo import YahooFinanceClient
from nycti.youtube import YouTubeTranscriptClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Nycti's isolated real-model benchmark suite locally."
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in LiveBenchmarkMode],
        default=LiveBenchmarkMode.ALL.value,
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Run one case; repeat this option to run a focused group.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--results", type=Path, default=Path("benchmarkresults.md"))
    parser.add_argument(
        "--traces",
        type=Path,
        default=Path("benchmarkresult_traces.md"),
    )
    args = parser.parse_args()
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    manifest = load_live_benchmark_manifest()
    if args.case_ids:
        requested_case_ids = set(args.case_ids)
        unknown_case_ids = requested_case_ids.difference(
            case.case_id for case in manifest.cases
        )
        if unknown_case_ids:
            raise ValueError(
                "Unknown benchmark case(s): " + ", ".join(sorted(unknown_case_ids))
            )
        manifest = replace(
            manifest,
            cases=tuple(
                case for case in manifest.cases if case.case_id in requested_case_ids
            ),
        )
    with tempfile.TemporaryDirectory(prefix="nycti-bench-") as temp_dir:
        settings = replace(
            Settings.from_env(),
            database_url=f"sqlite+aiosqlite:///{temp_dir}/benchmark.db",
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
        fixture_tool_runner = build_live_benchmark_fixture_tool_runner()

        async def execute_case(case: LiveBenchmarkCase) -> LiveBenchmarkExecution:
            fixture_now = (
                LIVE_BENCHMARK_FIXTURE_NOW
                if case.mode == LiveBenchmarkMode.FIXTURES
                else None
            )
            image_data_uri = load_live_benchmark_image_data_uri(manifest, case)
            reply, metrics = await bot._generate_reply(
                guild_id=None,
                channel_id=None,
                user_id=BENCHMARK_USER_ID,
                user_name="benchmark",
                user_global_name="benchmark",
                mentioned_user_ids=[],
                prompt=case.prompt,
                context_lines=[],
                image_attachment_urls=[image_data_uri] if image_data_uri else [],
                image_context_lines=(
                    ["- attached benchmark image: packaged vision fixture"]
                    if image_data_uri
                    else []
                ),
                source_message_id=None,
                request_started_at=time.perf_counter(),
                collect_latency_debug=True,
                include_memories=False,
                tool_runner=(
                    fixture_tool_runner
                    if case.mode == LiveBenchmarkMode.FIXTURES
                    else None
                ),
                isolated_benchmark=True,
                isolated_benchmark_now=fixture_now,
                isolated_benchmark_context=build_isolated_benchmark_context(
                    now=fixture_now,
                    personal_profile_block=case.context.personal_profile,
                    memories_block=case.context.memories,
                    memory_snapshot_block=case.context.memory_snapshot,
                    market_watchlist_block=case.context.market_watchlist,
                ),
            )
            return LiveBenchmarkExecution(answer=reply, metrics=metrics or {})

        try:
            result = await run_live_benchmark_suite(
                execute_case=execute_case,
                manifest=manifest,
                mode=args.mode,
                repeats=args.repeats,
            )
            _write_results(args.results, result)
            _write_raw_traces(args.traces, result)
            print(
                f"batch={result.batch_id} attempts={len(result.attempts)} "
                f"pass={result.count('pass')} fail={result.count('fail')} "
                f"error={result.count('error')} skip={result.count('skip')} "
                f"runtime_s={result.latency_ms / 1000:.1f}"
            )
        finally:
            await bot.close()
            await database.engine.dispose()
            await _close_llm_client(llm_client)


def _build_bot(settings: Settings, database: Database, llm_client: OpenAIClient) -> NyctiBot:
    memory_service = MemoryService(
        extractor=MemoryExtractor(settings, llm_client),
        retriever=MemoryRetriever(settings),
        llm_client=llm_client,
        embedding_model=settings.openai_embedding_model,
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
    )


def _write_results(path: Path, result) -> None:  # type: ignore[no-untyped-def]
    captured = datetime.now(timezone.utc).isoformat()
    header = (
        "# Benchmark Results\n\n"
        f"Revision: `{_revision()}`\n"
        f"Captured: `{captured}`\n"
        "Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and "
        "canaries use configured live providers.\n\n"
    )
    path.write_text(header + format_live_benchmark_batch_report(result), encoding="utf-8")


def _write_raw_traces(path: Path, result) -> None:  # type: ignore[no-untyped-def]
    raw_attempts: list[dict[str, object]] = []
    for attempt in result.attempts:
        if attempt.status not in {LiveBenchmarkStatus.FAIL, LiveBenchmarkStatus.ERROR}:
            continue
        execution = attempt.execution
        metrics = dict(execution.metrics) if execution is not None else {}
        for key in tuple(metrics):
            if key.startswith("_diagnostic_") and key.endswith("_json"):
                try:
                    metrics[key] = json.loads(str(metrics[key]))
                except json.JSONDecodeError:
                    pass
        raw_attempts.append(
            {
                "batch_id": result.batch_id,
                "manifest_version": result.manifest_version,
                "case_id": attempt.case.case_id,
                "attempt_index": attempt.attempt_index,
                "status": attempt.status.value,
                "latency_ms": attempt.latency_ms,
                "prompt": attempt.case.prompt,
                "description": attempt.case.description,
                "fixture_context": asdict(attempt.case.context),
                "image_fixture": attempt.case.image_fixture,
                "answer": execution.answer if execution is not None else "",
                "evaluation_reason": attempt.evaluation.reason,
                "checks": [asdict(check) for check in attempt.evaluation.checks],
                "metrics": metrics,
            }
        )
    body = json.dumps(raw_attempts, ensure_ascii=False, indent=2, default=str)
    path.write_text(
        "# Benchmark Result Traces\n\n"
        f"Revision: `{_revision()}`\n"
        f"Batch: `{result.batch_id}`\n"
        f"Manifest: `{result.manifest_version}`\n"
        f"Failed/error attempts: `{len(raw_attempts)}`\n\n"
        "Raw failed/error attempt dump from the isolated runner:\n\n"
        f"```json\n{body}\n```\n",
        encoding="utf-8",
    )


def _revision() -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return f"{commit} + working tree" if dirty else commit


async def _close_llm_client(client: OpenAIClient) -> None:
    await client.client.close()
    await client.embedding_client.close()
    if client.fallback_client is not None:
        await _close_llm_client(client.fallback_client)


if __name__ == "__main__":
    main()
