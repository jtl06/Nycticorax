from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from nycti.bot_support import BENCHMARK_USER_ID, build_isolated_benchmark_context
from nycti.config import Settings
from nycti.db.session import Database
from nycti.discord.live_benchmarks import format_live_benchmark_batch_report
from nycti.live_benchmarks import (
    LIVE_BENCHMARK_FIXTURE_NOW,
    LiveBenchmarkCase,
    LiveBenchmarkExecution,
    LiveBenchmarkMode,
    build_live_benchmark_fixture_tool_runner,
    load_live_benchmark_image_data_uri,
    load_live_benchmark_manifest,
    run_live_benchmark_suite,
)
from nycti.live_benchmark_discord import build_live_benchmark_message_context
from nycti.live_benchmark_baseline import (
    compare_live_benchmark_baseline,
    load_live_benchmark_baseline,
    write_live_benchmark_baseline,
)
from nycti.llm.client import OpenAIClient
from nycti.runtime import build_nycti_bot


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
    parser.add_argument(
        "--model",
        help="Optional foreground model override for A/B runs.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high"),
        help="Optional foreground reasoning-effort override.",
    )
    parser.add_argument(
        "--service-tier",
        choices=("default", "fast", "priority"),
        help="Optional OpenAI service-tier override.",
    )
    parser.add_argument("--results", type=Path, default=Path("benchmarkresults.md"))
    parser.add_argument(
        "--traces",
        type=Path,
        default=Path("benchmarkresult_traces.md"),
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        help="Write a machine-readable baseline from this run.",
    )
    parser.add_argument(
        "--compare-baseline",
        type=Path,
        help="Fail when quality or sufficiently sampled aggregate latency regresses.",
    )
    parser.add_argument(
        "--latency-tolerance-percent",
        type=float,
        default=15.0,
        help="Allowed avg/p90 latency increase when comparing a baseline (default: 15).",
    )
    args = parser.parse_args()
    if args.write_baseline and args.compare_baseline:
        parser.error("--write-baseline and --compare-baseline cannot be used together")
    if not 0 <= args.latency_tolerance_percent <= 200:
        parser.error("--latency-tolerance-percent must be between 0 and 200")
    passed = asyncio.run(_run(args))
    if not passed:
        raise SystemExit(1)


async def _run(args: argparse.Namespace) -> bool:
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
        selected = [case for case in manifest.cases if case.case_id in requested_case_ids]
        requested_case_ids.update(
            case.case_id
            for case in manifest.cases
            for target in selected
            if target.scenario_id
            and case.scenario_id == target.scenario_id
            and case.scenario_turn <= target.scenario_turn
        )
        manifest = replace(
            manifest,
            cases=tuple(
                case for case in manifest.cases if case.case_id in requested_case_ids
            ),
        )
    with tempfile.TemporaryDirectory(prefix="nycti-bench-") as temp_dir:
        base_settings = _load_settings_with_database(
            f"sqlite+aiosqlite:///{temp_dir}/benchmark.db"
        )
        settings = replace(
            base_settings,
            database_url=f"sqlite+aiosqlite:///{temp_dir}/benchmark.db",
            error_debug_channel_id=None,
            persist_bad_bot_diagnostics=False,
            openai_daily_token_budgets=(),
            openai_daily_token_fallback_model=None,
            openai_daily_token_fallback_reasoning_effort=None,
            openai_chat_model=args.model or base_settings.openai_chat_model,
            openai_reasoning_effort=(
                args.reasoning_effort or base_settings.openai_reasoning_effort
            ),
            openai_service_tier=(
                args.service_tier
                if args.service_tier is not None
                else base_settings.openai_service_tier
            ),
        )
        database = Database(settings)
        await database.init_models()
        llm_client = OpenAIClient(settings)
        bot = build_nycti_bot(settings=settings, database=database, llm_client=llm_client)

        async def execute_case(case: LiveBenchmarkCase) -> LiveBenchmarkExecution:
            case_started_at = time.perf_counter()
            fixture_now = (
                LIVE_BENCHMARK_FIXTURE_NOW
                if case.mode == LiveBenchmarkMode.FIXTURES
                else None
            )
            message_context = await build_live_benchmark_message_context(
                case,
                template_collector=getattr(bot, "_message_context_collector", None),
                now=fixture_now or datetime.now(timezone.utc),
            )
            image_data_uri = load_live_benchmark_image_data_uri(manifest, case)
            image_attachment_urls = list(message_context.image_attachment_urls)
            if image_data_uri:
                image_attachment_urls.append(image_data_uri)
            image_context_lines = list(message_context.image_context_lines)
            if image_data_uri:
                image_context_lines.append(
                    "- attached benchmark image: packaged vision fixture"
                )
            reply, metrics = await bot._generate_reply(
                guild_id=None,
                channel_id=None,
                user_id=BENCHMARK_USER_ID,
                user_name="benchmark",
                user_global_name="benchmark",
                mentioned_user_ids=[],
                prompt=case.prompt,
                context_lines=[],
                image_attachment_urls=image_attachment_urls,
                image_context_lines=image_context_lines,
                source_message_id=None,
                request_started_at=time.perf_counter(),
                collect_latency_debug=True,
                include_memories=False,
                tool_runner=(
                    build_live_benchmark_fixture_tool_runner(case.tool_fixtures)
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
                isolated_benchmark_context_lines=list(
                    message_context.context_lines
                ),
            )
            combined_metrics = {
                **(message_context.timing_metrics or {}),
                **(metrics or {}),
            }
            combined_metrics["context_fetch_ms"] = (
                message_context.timing_metrics or {}
            ).get("ctx_discord_ms", 0)
            combined_metrics["end_to_end_ms"] = max(
                round((time.perf_counter() - case_started_at) * 1000),
                0,
            )
            return LiveBenchmarkExecution(answer=reply, metrics=combined_metrics)

        try:
            result = await run_live_benchmark_suite(
                execute_case=execute_case,
                manifest=manifest,
                mode=args.mode,
                repeats=args.repeats,
            )
            _write_results(args.results, result)
            _write_raw_traces(args.traces, result)
            comparison_passed = True
            if args.write_baseline:
                write_live_benchmark_baseline(args.write_baseline, result)
                print(f"baseline_written={args.write_baseline}")
            if args.compare_baseline:
                comparison = compare_live_benchmark_baseline(
                    result,
                    load_live_benchmark_baseline(args.compare_baseline),
                    latency_tolerance=args.latency_tolerance_percent / 100,
                )
                comparison_passed = comparison.passed
                print(
                    "baseline_comparison="
                    + ("pass" if comparison.passed else "fail")
                )
                for failure in comparison.failures:
                    print(f"baseline_regression={failure}")
                for notice in comparison.notices:
                    print(f"baseline_note={notice}")
            print(
                f"batch={result.batch_id} attempts={len(result.attempts)} "
                f"pass={result.count('pass')} fail={result.count('fail')} "
                f"error={result.count('error')} skip={result.count('skip')} "
                f"runtime_s={result.latency_ms / 1000:.1f}"
            )
            return comparison_passed
        finally:
            await bot.close()
            await database.engine.dispose()
            await _close_llm_client(llm_client)


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
                "synthetic_discord": asdict(attempt.case.discord),
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
        f"Attempts: `{len(raw_attempts)}`\n\n"
        "Raw attempt dump from the isolated runner:\n\n"
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
