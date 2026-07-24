from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from io import BytesIO
import logging
import time
from typing import Any

try:
    import discord
    from discord import app_commands
except ModuleNotFoundError:  # pragma: no cover - test environments may omit discord.py
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]

from nycti.discord.common import SERVER_ONLY_MESSAGE, can_manage_guild
from nycti.bot_support import BENCHMARK_USER_ID, build_isolated_benchmark_context
from nycti.error_debug import send_error_debug_message
from nycti.live_benchmark_storage import (
    LiveBenchmarkAttemptInput,
    get_live_benchmark_failure_artifact,
    list_recent_live_benchmark_failures,
    save_live_benchmark_attempt,
)
from nycti.live_benchmarks import (
    LiveBenchmarkAttempt,
    LiveBenchmarkCase,
    LiveBenchmarkExecution,
    LIVE_BENCHMARK_FIXTURE_NOW,
    LiveBenchmarkMode,
    LiveBenchmarkStatus,
    LiveBenchmarkSuiteResult,
    build_live_benchmark_fixture_tool_runner,
    load_live_benchmark_manifest,
    run_live_benchmark_suite,
)

LOGGER = logging.getLogger(__name__)
LIVE_BENCHMARK_FAILURE_LIST_LIMIT = 25


def register_live_benchmark_commands(bot: Any, benchmark_group: Any) -> None:
    @benchmark_group.command(
        name="suite",
        description="Run isolated short-prompt evaluations against the real LLM.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        mode="Pinned fixture tools, changing live providers, or both",
        case_id="Optional exact case id from benchmarks/live_cases.json",
        repeats="Attempts per selected case (1-3)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Fixtures", value=LiveBenchmarkMode.FIXTURES.value),
            app_commands.Choice(name="Canaries", value=LiveBenchmarkMode.CANARIES.value),
            app_commands.Choice(name="All", value=LiveBenchmarkMode.ALL.value),
        ]
    )
    async def benchmark_suite(
        interaction: discord.Interaction,
        mode: str = LiveBenchmarkMode.FIXTURES.value,
        case_id: str | None = None,
        repeats: int = 1,
    ) -> None:
        request_context = await _benchmark_request_context(interaction)
        if request_context is None:
            return
        channel_id, user_id = request_context
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to run real-LLM benchmarks.",
                ephemeral=True,
            )
            return
        if not 1 <= repeats <= 3:
            await interaction.response.send_message(
                "`repeats` must be between 1 and 3.",
                ephemeral=True,
            )
            return
        request_key = (channel_id, user_id)
        if bot._active_requests.has_active(request_key):
            await interaction.response.send_message(
                "You already have an active request in this channel. Use `/cancel` to stop it.",
                ephemeral=True,
            )
            return
        suite_lock = _live_benchmark_lock(bot)
        if suite_lock.locked():
            await interaction.response.send_message(
                "Another live benchmark suite is already running.",
                ephemeral=True,
            )
            return

        await suite_lock.acquire()
        task: asyncio.Task[Any] | None = None
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
            if bot._active_requests.has_active(request_key):
                await _send_benchmark_notice(
                    interaction,
                    "A request started while this suite was being prepared. Try again later.",
                )
                return
            task = bot._active_requests.start(
                request_key,
                _run_suite(
                    bot,
                    mode=mode,
                    case_id=case_id,
                    repeats=repeats,
                ),
            )
            result, stored_ids = await task
        except asyncio.CancelledError:
            await _send_benchmark_notice(
                interaction,
                "Cancelled the suite. Completed attempts remain stored.",
            )
            return
        except (FileNotFoundError, ValueError) as exc:
            await _send_benchmark_notice(
                interaction,
                f"The live benchmark suite could not start: {exc}",
            )
            return
        except Exception:
            LOGGER.exception("Live benchmark suite crashed before producing a result.")
            await _send_benchmark_notice(
                interaction,
                "The live benchmark suite crashed. Check the bot logs.",
            )
            return
        finally:
            if task is not None:
                bot._active_requests.clear(request_key, task)
            suite_lock.release()

        summary = format_live_benchmark_suite_summary(result, stored_ids=stored_ids)
        report = format_live_benchmark_batch_report(result, stored_ids=stored_ids)
        await _send_suite_completion(
            interaction,
            summary=summary,
            report=report,
        )
        failed_count = result.count(LiveBenchmarkStatus.FAIL)
        error_count = result.count(LiveBenchmarkStatus.ERROR)
        if failed_count or error_count:
            LOGGER.warning(
                "Live benchmark batch %s completed with %s failure(s) and %s error(s).",
                result.batch_id,
                failed_count,
                error_count,
            )
            await send_error_debug_message(
                bot,
                channel_id=getattr(bot.settings, "error_debug_channel_id", None),
                content=summary,
                attachment_text=report,
                attachment_filename=f"nycti-live-benchmark-{result.batch_id[:12]}.md",
            )

    @benchmark_group.command(
        name="failures",
        description="List recent stored live-benchmark failures and errors.",
    )
    @app_commands.guild_only()
    @app_commands.describe(limit="Number of recent failures to show (1-25)")
    async def benchmark_failures(
        interaction: discord.Interaction,
        limit: int = 10,
    ) -> None:
        if await _benchmark_request_context(interaction) is None:
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to inspect benchmark failures.",
                ephemeral=True,
            )
            return
        bounded_limit = min(max(limit, 1), LIVE_BENCHMARK_FAILURE_LIST_LIMIT)
        failures = await list_recent_live_benchmark_failures(
            bot.database,
            limit=bounded_limit,
        )
        await interaction.response.send_message(
            format_live_benchmark_failures(failures),
            ephemeral=True,
        )

    @benchmark_group.command(
        name="trace",
        description="Download one stored redacted live-benchmark failure trace.",
    )
    @app_commands.guild_only()
    @app_commands.describe(failure_id="Numeric id shown by /benchmark failures")
    async def benchmark_trace(
        interaction: discord.Interaction,
        failure_id: int,
    ) -> None:
        if await _benchmark_request_context(interaction) is None:
            return
        if not can_manage_guild(interaction.user):
            await interaction.response.send_message(
                "You need `Manage Server` permission to inspect benchmark traces.",
                ephemeral=True,
            )
            return
        if failure_id < 1:
            await interaction.response.send_message(
                "`failure_id` must be a positive integer.",
                ephemeral=True,
            )
            return
        artifact = await get_live_benchmark_failure_artifact(
            bot.database,
            attempt_id=failure_id,
        )
        if artifact is None:
            await interaction.response.send_message(
                "No unexpired failure trace exists with that id.",
                ephemeral=True,
            )
            return
        attachment = discord.File(
            BytesIO(artifact.encode("utf-8")),
            filename=f"nycti-live-benchmark-{failure_id}.json",
        )
        await interaction.response.send_message(
            f"Redacted failure trace `#{failure_id}`.",
            file=attachment,
            ephemeral=True,
        )


async def _run_suite(
    bot: Any,
    *,
    mode: str,
    case_id: str | None,
    repeats: int,
) -> tuple[LiveBenchmarkSuiteResult, dict[str, int]]:
    manifest = load_live_benchmark_manifest()
    fixture_tool_runner = build_live_benchmark_fixture_tool_runner()
    stored_ids: dict[str, int] = {}

    async def execute_case(case: LiveBenchmarkCase) -> LiveBenchmarkExecution:
        fixture_now = (
            LIVE_BENCHMARK_FIXTURE_NOW
            if case.mode == LiveBenchmarkMode.FIXTURES
            else None
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
            image_attachment_urls=[],
            image_context_lines=[],
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
            ),
            persist_memory=False,
        )
        return LiveBenchmarkExecution(answer=reply, metrics=metrics or {})

    async def store_attempt(attempt: LiveBenchmarkAttempt) -> None:
        row_id = await save_live_benchmark_attempt(
            bot.database,
            attempt=build_live_benchmark_attempt_input(
                attempt,
                manifest_version=manifest.version,
            ),
        )
        stored_ids[attempt.attempt_id] = row_id

    result = await run_live_benchmark_suite(
        execute_case=execute_case,
        manifest=manifest,
        mode=mode,
        case_id=case_id,
        repeats=repeats,
        on_attempt=store_attempt,
        available_tools=lambda case: _available_tools_for_case(
            bot,
            fixture_tool_runner=fixture_tool_runner,
            case=case,
        ),
    )
    return result, stored_ids


def build_live_benchmark_attempt_input(
    attempt: LiveBenchmarkAttempt,
    *,
    manifest_version: int,
) -> LiveBenchmarkAttemptInput:
    execution = attempt.execution
    metrics = dict(execution.metrics) if execution is not None else {}
    failed_checks = attempt.evaluation.failed_checks
    if attempt.evaluation.reason:
        failed_checks = (*failed_checks, attempt.evaluation.reason)
    artifact: Mapping[str, object] | None = None
    if attempt.status in {LiveBenchmarkStatus.FAIL, LiveBenchmarkStatus.ERROR}:
        artifact_metrics = {
            key: value
            for key, value in metrics.items()
            if not key.startswith("_diagnostic_") and key != "agent_trace"
        }
        artifact = {
            "prompt": attempt.case.prompt,
            "description": attempt.case.description,
            "answer": execution.answer if execution is not None else "",
            "started_at": attempt.started_at,
            "evaluation_reason": attempt.evaluation.reason,
            "checks": [
                {
                    "id": check.check_id,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in attempt.evaluation.checks
            ],
            "metrics": artifact_metrics,
            "agent_trace": metrics.get("agent_trace", ""),
            "diagnostic_agent_messages_json": metrics.get(
                "_diagnostic_agent_messages_json",
                "[]",
            ),
            "diagnostic_agent_steps_json": metrics.get(
                "_diagnostic_agent_steps_json",
                "[]",
            ),
            "tool_schemas_json": metrics.get(
                "_diagnostic_tool_schemas_json",
                "[]",
            ),
        }
        if not attempt.case.context.is_empty:
            artifact["fixture_context"] = {
                "personal_profile": attempt.case.context.personal_profile,
                "memories": attempt.case.context.memories,
            }
    called_tools = execution.resolved_called_tools if execution is not None else ()
    return LiveBenchmarkAttemptInput(
        batch_id=attempt.batch_id,
        suite_version=str(manifest_version),
        case_id=attempt.case.case_id,
        attempt_index=attempt.attempt_index,
        mode=attempt.case.mode.value,
        status=attempt.status.value,
        score=attempt.evaluation.score,
        max_score=attempt.evaluation.max_score,
        failed_checks=failed_checks,
        agent_run_id=_optional_metric(metrics, "agent_run_id"),
        model=_optional_metric(metrics, "active_chat_model", "chat_model"),
        provider=_optional_metric(metrics, "active_chat_provider"),
        profile=_optional_metric(metrics, "answer_profile"),
        prompt_tokens=_int_metric(metrics, "chat_prompt_tokens"),
        completion_tokens=_int_metric(metrics, "chat_completion_tokens"),
        total_tokens=_int_metric(metrics, "agent_total_tokens", "chat_total_tokens"),
        latency_ms=attempt.latency_ms,
        tools_called=called_tools,
        error=(
            attempt.evaluation.reason
            if attempt.status == LiveBenchmarkStatus.ERROR
            else None
        ),
        failure_artifact=artifact,
        created_at=attempt.started_at,
    )


def format_live_benchmark_suite_summary(
    result: LiveBenchmarkSuiteResult,
    *,
    stored_ids: Mapping[str, int] | None = None,
) -> str:
    ids = stored_ids or {}
    lines = [
        f"**Live LLM benchmark `{result.batch_id[:12]}`**",
        (
            f"mode `{result.mode.value}` · {len(result.attempts)} attempt(s) · "
            f"{result.count('pass')} pass · {result.count('fail')} fail · "
            f"{result.count('error')} error · {result.count('skip')} skip · "
            f"{result.latency_ms / 1000:.1f}s"
        ),
    ]
    for attempt in result.attempts:
        score = f"{attempt.evaluation.score}/{attempt.evaluation.max_score}"
        row_id = ids.get(attempt.attempt_id)
        stored = f" · log `#{row_id}`" if row_id is not None else ""
        lines.append(
            f"- `{attempt.case.case_id}` #{attempt.attempt_index}: "
            f"**{attempt.status.value.upper()}** {score}{stored}"
        )
    if result.observer_errors:
        lines.append(
            f"Storage warning: {len(result.observer_errors)} attempt record(s) could not be saved."
        )
    if result.count(LiveBenchmarkStatus.FAIL) or result.count(LiveBenchmarkStatus.ERROR):
        lines.append("Use `/benchmark failures` and `/benchmark trace` to inspect saved failures.")
    return _bounded_message("\n".join(lines))


def format_live_benchmark_failures(failures: Collection[Any]) -> str:
    if not failures:
        return "No unexpired live-benchmark failures are stored."
    lines = ["**Recent live-benchmark failures**"]
    for failure in failures:
        model = failure.model or "unknown model"
        lines.append(
            f"- `#{failure.id}` **{failure.status.upper()}** `{failure.case_id}` "
            f"#{failure.attempt_index} · {failure.score:g}/{failure.max_score:g} · "
            f"{failure.latency_ms / 1000:.1f}s · `{model}`"
        )
    lines.append("Download one with `/benchmark trace failure_id:<id>`.")
    return _bounded_message("\n".join(lines))


def format_live_benchmark_batch_report(
    result: LiveBenchmarkSuiteResult,
    *,
    stored_ids: Mapping[str, int] | None = None,
) -> str:
    ids = stored_ids or {}
    lines = [
        "# Nycti Live LLM Benchmark",
        "",
        f"- Batch: `{result.batch_id}`",
        f"- Manifest version: `{result.manifest_version}`",
        f"- Mode: `{result.mode.value}`",
        f"- Started: `{result.started_at.isoformat()}`",
        f"- Runtime: `{result.latency_ms / 1000:.1f}s`",
        "",
        (
            "| Case | Attempt | Status | Score | Model | Provider | Tools called | "
            "Turns | Tokens | Stop reason | Log ID | Runtime |"
        ),
        "| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for attempt in result.attempts:
        row_id = ids.get(attempt.attempt_id)
        execution = attempt.execution
        metrics = execution.metrics if execution is not None else {}
        tools = (
            ", ".join(execution.resolved_called_tools)
            if execution is not None
            else ""
        )
        lines.append(
            f"| `{attempt.case.case_id}` | {attempt.attempt_index} | "
            f"{attempt.status.value.upper()} | {attempt.evaluation.score}/{attempt.evaluation.max_score} | "
            f"{_report_metric(metrics, 'active_chat_model', 'chat_model')} | "
            f"{_report_metric(metrics, 'active_chat_provider')} | "
            f"{_markdown_table_cell(tools)} | "
            f"{_report_int_metric(metrics, 'agent_model_turn_count')} | "
            f"{_report_int_metric(metrics, 'agent_total_tokens', 'chat_total_tokens')} | "
            f"{_report_metric(metrics, 'agent_stop_reason')} | "
            f"{row_id if row_id is not None else '-'} | {attempt.latency_ms / 1000:.1f}s |"
        )
    noteworthy = [
        attempt
        for attempt in result.attempts
        if attempt.status in {LiveBenchmarkStatus.FAIL, LiveBenchmarkStatus.ERROR}
    ]
    if noteworthy:
        lines.extend(("", "## Failures and errors", ""))
        for attempt in noteworthy:
            lines.append(
                f"- `{attempt.case.case_id}` attempt {attempt.attempt_index}: "
                + "; ".join(
                    (*attempt.evaluation.failed_checks, attempt.evaluation.reason)
                ).strip("; ")
            )
    if result.observer_errors:
        lines.extend(("", "## Storage warnings", ""))
        lines.extend(f"- {error}" for error in result.observer_errors)
    return "\n".join(lines).strip() + "\n"


async def _send_suite_completion(
    interaction: Any,
    *,
    summary: str,
    report: str,
) -> None:
    is_expired = getattr(interaction, "is_expired", None)
    expired = bool(is_expired()) if callable(is_expired) else False
    if not expired:
        try:
            await interaction.followup.send(
                summary,
                file=_batch_report_file(report),
                ephemeral=True,
            )
            return
        except (discord.HTTPException, discord.NotFound):
            LOGGER.warning(
                "Benchmark interaction followup expired; falling back to its channel.",
                exc_info=True,
            )
    channel = getattr(interaction, "channel", None)
    send = getattr(channel, "send", None)
    if not callable(send):
        LOGGER.error("Could not deliver completed benchmark batch %s.", summary[:120])
        return
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    mention = f"<@{user_id}> " if isinstance(user_id, int) else ""
    await send(
        _bounded_message(f"{mention}{summary}", limit=1950),
        file=_batch_report_file(report),
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=True,
            replied_user=False,
        ),
    )


async def _send_benchmark_notice(interaction: Any, message: str) -> None:
    is_expired = getattr(interaction, "is_expired", None)
    expired = bool(is_expired()) if callable(is_expired) else False
    if not expired:
        try:
            await interaction.followup.send(message, ephemeral=True)
            return
        except (discord.HTTPException, discord.NotFound):
            LOGGER.warning(
                "Benchmark interaction followup expired while sending a notice.",
                exc_info=True,
            )
    channel = getattr(interaction, "channel", None)
    send = getattr(channel, "send", None)
    if not callable(send):
        LOGGER.error("Could not deliver benchmark notice: %s", message[:120])
        return
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    mention = f"<@{user_id}> " if isinstance(user_id, int) else ""
    await send(
        _bounded_message(f"{mention}{message}", limit=1950),
        allowed_mentions=discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=True,
            replied_user=False,
        ),
    )


def _batch_report_file(report: str) -> discord.File:
    return discord.File(
        BytesIO(report.encode("utf-8")),
        filename="nycti-live-benchmark-results.md",
    )


def _available_tools_for_case(
    bot: Any,
    *,
    fixture_tool_runner: Any,
    case: LiveBenchmarkCase,
) -> Collection[str] | None:
    runner = (
        fixture_tool_runner
        if case.mode == LiveBenchmarkMode.FIXTURES
        else getattr(bot._chat_orchestrator, "tool_runner", None)
    )
    availability = getattr(getattr(runner, "executor", None), "available_tool_names", None)
    if not callable(availability):
        return None
    return availability(guild_id=None, channel_id=None, source_message_id=None)


async def _benchmark_request_context(interaction: Any) -> tuple[int, int] | None:
    if interaction.channel is None or interaction.user is None or interaction.guild is None:
        await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
        return None
    channel_id = getattr(interaction.channel, "id", None)
    user_id = getattr(interaction.user, "id", None)
    if not isinstance(channel_id, int) or not isinstance(user_id, int):
        await interaction.response.send_message(SERVER_ONLY_MESSAGE, ephemeral=True)
        return None
    return channel_id, user_id


def _live_benchmark_lock(bot: Any) -> asyncio.Lock:
    lock = getattr(bot, "_live_benchmark_lock", None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    bot._live_benchmark_lock = lock
    return lock


def _optional_metric(
    metrics: Mapping[str, int | str],
    *names: str,
) -> str | None:
    for name in names:
        value = str(metrics.get(name, "") or "").strip()
        if value and value.casefold() not in {"none", "(none)", "unknown"}:
            return value
    return None


def _int_metric(metrics: Mapping[str, int | str], *names: str) -> int:
    for name in names:
        if name not in metrics:
            continue
        try:
            return max(int(metrics[name]), 0)
        except (TypeError, ValueError):
            continue
    return 0


def _report_metric(
    metrics: Mapping[str, int | str],
    *names: str,
) -> str:
    for name in names:
        if name not in metrics:
            continue
        value = str(metrics[name]).strip()
        if value:
            return _markdown_table_cell(value)
    return "-"


def _report_int_metric(metrics: Mapping[str, int | str], *names: str) -> str:
    for name in names:
        if name not in metrics:
            continue
        try:
            return f"{max(int(metrics[name]), 0):,}"
        except (TypeError, ValueError):
            continue
    return "-"


def _markdown_table_cell(value: object) -> str:
    rendered = " ".join(str(value).split()).replace("|", "\\|")
    return rendered or "-"


def _bounded_message(text: str, limit: int = 1950) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n… output truncated"
    return text[: max(limit - len(suffix), 0)].rstrip() + suffix
