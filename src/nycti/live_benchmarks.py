from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
import math
from pathlib import Path
import re
import statistics
import sysconfig
import time
from typing import TypeAlias
from urllib.parse import urlsplit
from uuid import uuid4

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    DEEP_RESEARCH_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)
from nycti.live_benchmark_fixture_tools import (
    execute_fixture_annual_performance,
    execute_fixture_browser_extract,
    execute_fixture_channel_context,
    execute_fixture_deep_research,
    execute_fixture_image_search,
    execute_fixture_memory_search,
    execute_fixture_price_history,
    execute_fixture_python,
    execute_fixture_quote,
    execute_fixture_url_extract,
    execute_fixture_web,
    execute_fixture_youtube_transcript,
)
from nycti.live_benchmark_regex import LiveBenchmarkRegexGroup
from nycti.live_benchmark_diagnostics import (
    extract_called_tools,
    extract_successful_tools,
    grounding_passed,
    infrastructure_error,
    numeric_metric,
    observed_tool_call_count,
)

LOGGER = logging.getLogger(__name__)

MAX_LIVE_BENCHMARK_PROMPT_CHARS = 120
MAX_LIVE_BENCHMARK_CONTEXT_CHARS = 2_000
MAX_LIVE_BENCHMARK_DISCORD_MESSAGES = 20
MAX_LIVE_BENCHMARK_DISCORD_MESSAGE_CHARS = 600
MAX_LIVE_BENCHMARK_DISCORD_AGE_MINUTES = 60 * 24 * 30
MAX_LIVE_BENCHMARK_REPEATS = 3
MAX_LIVE_BENCHMARK_IMAGE_BYTES = 5 * 1024 * 1024
LIVE_BENCHMARK_FIXTURE_NOW = datetime(2026, 7, 10, 15, 30, tzinfo=UTC)
_SOURCE_LIVE_BENCHMARK_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "live_cases.json"
)
_INSTALLED_LIVE_BENCHMARK_MANIFEST_PATH = (
    Path(sysconfig.get_path("data"))
    / "share"
    / "nycti"
    / "benchmarks"
    / "live_cases.json"
)
DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH = next(
    (
        candidate
        for candidate in (
            _SOURCE_LIVE_BENCHMARK_MANIFEST_PATH,
            _INSTALLED_LIVE_BENCHMARK_MANIFEST_PATH,
        )
        if candidate.is_file()
    ),
    _INSTALLED_LIVE_BENCHMARK_MANIFEST_PATH,
)

_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*<?(?P<url>https?://[^)\s>]+)>?"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*\)",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[[^\]\r\n]*\]\(\s*<?https?://[^)\s>]+>?"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*\)",
    re.IGNORECASE,
)
_RAW_URL_RE = re.compile(r"https?://[^\s<>()\]]+", re.IGNORECASE)
_EVIDENCE_MARKERS_ONLY_RE = re.compile(
    r"(?:\[E-[A-Z0-9]{1,64}\]\s*)*",
    re.IGNORECASE,
)
_IMAGE_PATH_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp")


class LiveBenchmarkMode(StrEnum):
    FIXTURES = "fixtures"
    CANARIES = "canaries"
    ALL = "all"


class LiveBenchmarkStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class LiveBenchmarkChecks:
    required_tools: tuple[str, ...] = ()
    required_attempted_tools: tuple[str, ...] = ()
    required_any_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    answer_regex: tuple[str, ...] = ()
    answer_regex_groups: tuple[LiveBenchmarkRegexGroup, ...] = ()
    forbidden_answer_regex: tuple[str, ...] = ()
    metric_min: Mapping[str, float] = field(default_factory=dict)
    metric_max: Mapping[str, float] = field(default_factory=dict)
    metric_equals: Mapping[str, int | str] = field(default_factory=dict)
    grounding_required: bool = False
    image_delivery_required: bool = False
    max_answer_chars: int | None = None
    max_tool_calls: int | None = None

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Tools which must all be available before this case is meaningful."""
        return frozenset((*self.required_tools, *self.required_attempted_tools))


@dataclass(frozen=True, slots=True)
class LiveBenchmarkPromptContext:
    """Synthetic prompt context for fixture cases; never sourced from production data."""

    personal_profile: str = ""
    memories: str = ""
    memory_snapshot: str = ""
    market_watchlist: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.personal_profile
            or self.memories
            or self.memory_snapshot
            or self.market_watchlist
        )


@dataclass(frozen=True, slots=True)
class LiveBenchmarkDiscordMessage:
    """One synthetic Discord message; manifests list recent messages oldest first."""

    author: str
    content: str
    minutes_ago: int


@dataclass(frozen=True, slots=True)
class LiveBenchmarkDiscordContext:
    """Synthetic channel state; reply_chain is ordered from nearest reply outward."""

    recent_messages: tuple[LiveBenchmarkDiscordMessage, ...] = ()
    reply_chain: tuple[LiveBenchmarkDiscordMessage, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.recent_messages or self.reply_chain)


@dataclass(frozen=True, slots=True)
class LiveBenchmarkCase:
    case_id: str
    mode: LiveBenchmarkMode
    prompt: str
    checks: LiveBenchmarkChecks
    description: str = ""
    context: LiveBenchmarkPromptContext = field(default_factory=LiveBenchmarkPromptContext)
    discord: LiveBenchmarkDiscordContext = field(default_factory=LiveBenchmarkDiscordContext)
    image_fixture: str = ""


@dataclass(frozen=True, slots=True)
class LiveBenchmarkManifest:
    version: int
    cases: tuple[LiveBenchmarkCase, ...]
    description: str = ""
    mode_defaults: Mapping[LiveBenchmarkMode, LiveBenchmarkChecks] = field(default_factory=dict)
    asset_root: Path | None = None

    def get_case(self, case_id: str) -> LiveBenchmarkCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise KeyError(case_id)


@dataclass(frozen=True, slots=True)
class LiveBenchmarkExecution:
    """One real foreground-model result supplied by the runtime integration."""

    answer: str
    metrics: Mapping[str, int | str] = field(default_factory=dict)
    called_tools: tuple[str, ...] | None = None
    successful_tools: tuple[str, ...] | None = None
    error: str = ""

    @property
    def resolved_called_tools(self) -> tuple[str, ...]:
        if self.called_tools is not None:
            return self.called_tools
        return extract_called_tools(self.metrics)

    @property
    def resolved_successful_tools(self) -> tuple[str, ...]:
        if self.successful_tools is not None:
            return self.successful_tools
        return extract_successful_tools(self.metrics)


@dataclass(frozen=True, slots=True)
class LiveBenchmarkCheckResult:
    check_id: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class LiveBenchmarkEvaluation:
    status: LiveBenchmarkStatus
    checks: tuple[LiveBenchmarkCheckResult, ...] = ()
    reason: str = ""

    @property
    def score(self) -> int:
        return sum(check.passed for check in self.checks)

    @property
    def max_score(self) -> int:
        return len(self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(
            f"{check.check_id}: {check.detail}"
            for check in self.checks
            if not check.passed
        )


@dataclass(frozen=True, slots=True)
class LiveBenchmarkAttempt:
    batch_id: str
    case: LiveBenchmarkCase
    attempt_index: int
    evaluation: LiveBenchmarkEvaluation
    started_at: datetime
    latency_ms: int
    execution: LiveBenchmarkExecution | None = None

    @property
    def status(self) -> LiveBenchmarkStatus:
        return self.evaluation.status

    @property
    def attempt_id(self) -> str:
        return f"{self.batch_id}:{self.case.case_id}:{self.attempt_index}"


@dataclass(frozen=True, slots=True)
class LiveBenchmarkSuiteResult:
    batch_id: str
    manifest_version: int
    mode: LiveBenchmarkMode
    attempts: tuple[LiveBenchmarkAttempt, ...]
    started_at: datetime
    latency_ms: int
    observer_errors: tuple[str, ...] = ()

    def count(self, status: LiveBenchmarkStatus | str) -> int:
        normalized = LiveBenchmarkStatus(status)
        return sum(attempt.status == normalized for attempt in self.attempts)

    @property
    def passed(self) -> bool:
        return bool(self.attempts) and all(
            attempt.status in {LiveBenchmarkStatus.PASS, LiveBenchmarkStatus.SKIP}
            for attempt in self.attempts
        )


@dataclass(frozen=True, slots=True)
class LiveBenchmarkAggregate:
    attempt_count: int
    skipped_count: int
    pass_count: int
    fail_count: int
    error_count: int
    pass_rate: float
    check_score: int
    check_max_score: int
    check_rate: float
    latency_avg_ms: int
    latency_p50_ms: int
    latency_p90_ms: int
    latency_max_ms: int
    reply_generation_avg_ms: int
    model_turns_avg: float
    tool_calls_avg: float
    tokens_avg: int


LiveBenchmarkExecutor: TypeAlias = Callable[
    [LiveBenchmarkCase], Awaitable[LiveBenchmarkExecution]
]
LiveBenchmarkObserver: TypeAlias = Callable[[LiveBenchmarkAttempt], Awaitable[None]]
AvailableToolResolver: TypeAlias = Collection[str] | Callable[
    [LiveBenchmarkCase], Collection[str] | None
]


def load_live_benchmark_manifest(
    path: str | Path | None = None,
) -> LiveBenchmarkManifest:
    manifest_path = Path(path) if path is not None else DEFAULT_LIVE_BENCHMARK_MANIFEST_PATH
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Live benchmark manifest not found at {manifest_path}. "
            "Deploy the repository's benchmarks/live_cases.json file with the application."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Live benchmark manifest {manifest_path} is not valid JSON: {exc}"
        ) from exc
    return replace(
        parse_live_benchmark_manifest(raw),
        asset_root=manifest_path.resolve().parent,
    )


def load_live_benchmark_image_data_uri(
    manifest: LiveBenchmarkManifest,
    case: LiveBenchmarkCase,
) -> str | None:
    """Load one packaged benchmark image without depending on an external URL."""
    if not case.image_fixture:
        return None
    if manifest.asset_root is None:
        raise ValueError(
            f"Live benchmark case {case.case_id} has an image fixture but no asset root"
        )
    asset_root = manifest.asset_root.resolve()
    image_path = (asset_root / case.image_fixture).resolve()
    if not image_path.is_relative_to(asset_root):
        raise ValueError(
            f"Live benchmark case {case.case_id} image fixture escapes the asset root"
        )
    try:
        image_bytes = image_path.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Live benchmark image fixture not found at {image_path}"
        ) from exc
    if len(image_bytes) > MAX_LIVE_BENCHMARK_IMAGE_BYTES:
        raise ValueError(
            f"Live benchmark image fixture {image_path} exceeds "
            f"{MAX_LIVE_BENCHMARK_IMAGE_BYTES} bytes"
        )
    mime_type = {
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }[image_path.suffix.casefold()]
    payload = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def parse_live_benchmark_manifest(raw: object) -> LiveBenchmarkManifest:
    from nycti.live_benchmark_manifest import parse_live_benchmark_manifest as parse

    return parse(raw)


def evaluate_live_benchmark(
    case: LiveBenchmarkCase,
    execution: LiveBenchmarkExecution,
) -> LiveBenchmarkEvaluation:
    infrastructure_failure = infrastructure_error(
        error=execution.error,
        metrics=execution.metrics,
    )
    if infrastructure_failure:
        return LiveBenchmarkEvaluation(
            status=LiveBenchmarkStatus.ERROR,
            reason=infrastructure_failure,
        )

    answer = execution.answer.strip()
    metrics = execution.metrics
    called_tools = execution.resolved_called_tools
    called_set = frozenset(called_tools)
    successful_set = frozenset(execution.resolved_successful_tools)
    checks: list[LiveBenchmarkCheckResult] = []

    checks.append(
        _check(
            "answer:not_empty",
            bool(answer),
            "answer was empty" if not answer else "answer was non-empty",
        )
    )
    for index, pattern in enumerate(case.checks.answer_regex, start=1):
        matched = re.search(pattern, answer, re.IGNORECASE | re.DOTALL) is not None
        checks.append(
            _check(
                f"answer:matches:{index}",
                matched,
                f"required pattern {pattern!r} was {'found' if matched else 'missing'}",
            )
        )
    for index, group in enumerate(case.checks.answer_regex_groups, start=1):
        flags = re.DOTALL if group.case_sensitive else re.IGNORECASE | re.DOTALL
        matched_patterns = tuple(
            pattern
            for pattern in group.patterns
            if re.search(pattern, answer, flags) is not None
        )
        checks.append(
            _check(
                f"answer:matches_group:{index}",
                len(matched_patterns) >= group.minimum,
                (
                    f"matched {len(matched_patterns)} of {len(group.patterns)} distinct patterns; "
                    f"required at least {group.minimum}"
                ),
            )
        )
    for index, pattern in enumerate(case.checks.forbidden_answer_regex, start=1):
        matched = re.search(pattern, answer, re.IGNORECASE | re.DOTALL) is not None
        checks.append(
            _check(
                f"answer:forbidden:{index}",
                not matched,
                f"forbidden pattern {pattern!r} was {'found' if matched else 'absent'}",
            )
        )
    if case.checks.image_delivery_required:
        image_delivered = _has_deliverable_image(answer, metrics=metrics)
        checks.append(
            _check(
                "answer:image_delivery",
                image_delivered,
                (
                    "answer contained a Markdown image or bare image URL"
                    if image_delivered
                    else "answer contained no Markdown image or bare image URL"
                ),
            )
        )

    for tool_name in case.checks.required_tools:
        present = tool_name in successful_set
        checks.append(
            _check(
                f"tool:succeeded:{tool_name}",
                present,
                f"{tool_name} {'succeeded' if present else 'did not succeed'}",
            )
        )
    for tool_name in case.checks.required_attempted_tools:
        present = tool_name in called_set
        checks.append(
            _check(
                f"tool:attempted:{tool_name}",
                present,
                f"{tool_name} was {'attempted' if present else 'not attempted'}",
            )
        )
    if case.checks.required_any_tools:
        observed = tuple(
            name for name in case.checks.required_any_tools if name in successful_set
        )
        checks.append(
            _check(
                "tool:called_any",
                bool(observed),
                (
                    "succeeded with " + ", ".join(observed)
                    if observed
                    else "none succeeded: " + ", ".join(case.checks.required_any_tools)
                ),
            )
        )
    for tool_name in case.checks.forbidden_tools:
        present = tool_name in called_set
        checks.append(
            _check(
                f"tool:not_called:{tool_name}",
                not present,
                f"{tool_name} was {'called' if present else 'not called'}",
            )
        )

    for metric_name, expected_minimum in case.checks.metric_min.items():
        observed_numeric = numeric_metric(metrics.get(metric_name))
        passed = observed_numeric is not None and observed_numeric >= expected_minimum
        checks.append(
            _check(
                f"metric:min:{metric_name}",
                passed,
                f"observed {metrics.get(metric_name)!r}; required at least {expected_minimum:g}",
            )
        )
    for metric_name, expected_maximum in case.checks.metric_max.items():
        observed_numeric = numeric_metric(metrics.get(metric_name))
        passed = observed_numeric is not None and observed_numeric <= expected_maximum
        checks.append(
            _check(
                f"metric:max:{metric_name}",
                passed,
                f"observed {metrics.get(metric_name)!r}; required at most {expected_maximum:g}",
            )
        )
    for metric_name, expected_value in case.checks.metric_equals.items():
        observed_metric = metrics.get(metric_name)
        passed = observed_metric == expected_value or (
            isinstance(observed_metric, str) and observed_metric == str(expected_value)
        )
        checks.append(
            _check(
                f"metric:equals:{metric_name}",
                passed,
                f"observed {observed_metric!r}; required {expected_value!r}",
            )
        )

    if case.checks.grounding_required:
        grounded = grounding_passed(metrics)
        checks.append(
            _check(
                "grounding:valid",
                grounded,
                (
                    "grounded-answer metrics passed"
                    if grounded
                    else "no successful grounded-answer metric was recorded"
                ),
            )
        )
    if case.checks.max_answer_chars is not None:
        limit = case.checks.max_answer_chars
        checks.append(
            _check(
                "answer:max_chars",
                len(answer) <= limit,
                f"answer length was {len(answer)}; limit is {limit}",
            )
        )
    if case.checks.max_tool_calls is not None:
        observed_calls = observed_tool_call_count(execution.metrics, called_tools)
        limit = case.checks.max_tool_calls
        checks.append(
            _check(
                "tool:max_calls",
                observed_calls <= limit,
                f"tool call count was {observed_calls}; limit is {limit}",
            )
        )

    status = (
        LiveBenchmarkStatus.PASS
        if all(check.passed for check in checks)
        else LiveBenchmarkStatus.FAIL
    )
    return LiveBenchmarkEvaluation(status=status, checks=tuple(checks))


def aggregate_live_benchmark_suite(
    result: LiveBenchmarkSuiteResult,
) -> LiveBenchmarkAggregate:
    active_attempts = tuple(
        attempt
        for attempt in result.attempts
        if attempt.status != LiveBenchmarkStatus.SKIP
    )
    evaluated_attempts = tuple(
        attempt for attempt in active_attempts if attempt.evaluation.max_score > 0
    )
    latencies = [attempt.latency_ms for attempt in active_attempts]
    reply_latencies = _attempt_numeric_metrics(
        active_attempts,
        "reply_generation_ms",
    )
    turns = _attempt_numeric_metrics(active_attempts, "agent_model_turn_count")
    tool_calls = _attempt_numeric_metrics(active_attempts, "agent_tool_call_count")
    tokens = _attempt_numeric_metrics(
        active_attempts,
        "agent_total_tokens",
        "chat_total_tokens",
    )
    check_score = sum(attempt.evaluation.score for attempt in evaluated_attempts)
    check_max_score = sum(
        attempt.evaluation.max_score for attempt in evaluated_attempts
    )
    pass_count = sum(
        attempt.status == LiveBenchmarkStatus.PASS for attempt in active_attempts
    )
    return LiveBenchmarkAggregate(
        attempt_count=len(active_attempts),
        skipped_count=result.count(LiveBenchmarkStatus.SKIP),
        pass_count=pass_count,
        fail_count=result.count(LiveBenchmarkStatus.FAIL),
        error_count=result.count(LiveBenchmarkStatus.ERROR),
        pass_rate=_ratio(pass_count, len(active_attempts)),
        check_score=check_score,
        check_max_score=check_max_score,
        check_rate=_ratio(check_score, check_max_score),
        latency_avg_ms=_average_int(latencies),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p90_ms=_percentile(latencies, 0.90),
        latency_max_ms=max(latencies, default=0),
        reply_generation_avg_ms=_average_int(reply_latencies),
        model_turns_avg=_average_float(turns),
        tool_calls_avg=_average_float(tool_calls),
        tokens_avg=_average_int(tokens),
    )


async def run_live_benchmark_suite(
    *,
    execute_case: LiveBenchmarkExecutor,
    manifest: LiveBenchmarkManifest | None = None,
    mode: LiveBenchmarkMode | str = LiveBenchmarkMode.FIXTURES,
    case_id: str | None = None,
    repeats: int = 1,
    on_attempt: LiveBenchmarkObserver | None = None,
    available_tools: AvailableToolResolver | None = None,
    batch_id: str | None = None,
) -> LiveBenchmarkSuiteResult:
    """Run selected real-LLM cases sequentially and report every attempt.

    The callback is intentionally injected: production uses Nycti's real
    foreground model while unit tests remain deterministic and offline.
    """

    selected_mode = LiveBenchmarkMode(mode)
    if (
        not isinstance(repeats, int)
        or isinstance(repeats, bool)
        or not 1 <= repeats <= MAX_LIVE_BENCHMARK_REPEATS
    ):
        raise ValueError(
            f"repeats must be between 1 and {MAX_LIVE_BENCHMARK_REPEATS}"
        )
    active_manifest = manifest or load_live_benchmark_manifest()
    selected_cases = [
        case
        for case in active_manifest.cases
        if selected_mode == LiveBenchmarkMode.ALL or case.mode == selected_mode
    ]
    if case_id is not None:
        selected_cases = [case for case in selected_cases if case.case_id == case_id]
    if not selected_cases:
        qualifier = f" with id {case_id!r}" if case_id is not None else ""
        raise ValueError(f"No {selected_mode} live benchmark cases found{qualifier}")

    effective_batch_id = (batch_id or uuid4().hex).strip()
    if not effective_batch_id:
        raise ValueError("batch_id must not be empty")
    suite_started_at = datetime.now(UTC)
    suite_started = time.perf_counter()
    attempts: list[LiveBenchmarkAttempt] = []
    observer_errors: list[str] = []

    for case in selected_cases:
        for attempt_index in range(1, repeats + 1):
            attempt_started_at = datetime.now(UTC)
            attempt_started = time.perf_counter()
            unavailable_reason = _unavailable_reason(
                case,
                _available_tools_for_case(available_tools, case),
            )
            execution: LiveBenchmarkExecution | None = None
            if unavailable_reason:
                evaluation = LiveBenchmarkEvaluation(
                    status=LiveBenchmarkStatus.SKIP,
                    reason=unavailable_reason,
                )
            else:
                try:
                    execution = await execute_case(case)
                    if not isinstance(execution, LiveBenchmarkExecution):
                        raise TypeError(
                            "execute_case must return LiveBenchmarkExecution"
                        )
                    evaluation = evaluate_live_benchmark(case, execution)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.exception(
                        "Live benchmark execution failed for %s attempt %s.",
                        case.case_id,
                        attempt_index,
                    )
                    evaluation = LiveBenchmarkEvaluation(
                        status=LiveBenchmarkStatus.ERROR,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
            attempt = LiveBenchmarkAttempt(
                batch_id=effective_batch_id,
                case=case,
                attempt_index=attempt_index,
                evaluation=evaluation,
                started_at=attempt_started_at,
                latency_ms=_elapsed_ms(attempt_started),
                execution=execution,
            )
            attempts.append(attempt)
            if on_attempt is not None:
                observer_task: asyncio.Future[None] = asyncio.ensure_future(
                    on_attempt(attempt)
                )
                try:
                    await asyncio.shield(observer_task)
                except asyncio.CancelledError:
                    await _finish_observer_after_cancellation(
                        observer_task,
                        attempt_id=attempt.attempt_id,
                    )
                    raise
                except Exception as exc:
                    observer_errors.append(
                        f"{attempt.attempt_id}: {type(exc).__name__}: {exc}"
                    )

    return LiveBenchmarkSuiteResult(
        batch_id=effective_batch_id,
        manifest_version=active_manifest.version,
        mode=selected_mode,
        attempts=tuple(attempts),
        started_at=suite_started_at,
        latency_ms=_elapsed_ms(suite_started),
        observer_errors=tuple(observer_errors),
    )


LIVE_BENCHMARK_FIXTURE_TOOL_NAMES = frozenset(
    {
        WEB_SEARCH_TOOL_NAME,
        EXTRACT_URL_TOOL_NAME,
        BROWSER_EXTRACT_TOOL_NAME,
        PYTHON_EXEC_TOOL_NAME,
        STOCK_QUOTE_TOOL_NAME,
        PRICE_HISTORY_TOOL_NAME,
        ANNUAL_PERFORMANCE_TOOL_NAME,
        YOUTUBE_TRANSCRIPT_TOOL_NAME,
        IMAGE_SEARCH_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        GET_CHANNEL_CONTEXT_TOOL_NAME,
        DEEP_RESEARCH_TOOL_NAME,
    }
)
class LiveBenchmarkFixtureExecutor:
    """Deterministic evidence providers around a real foreground LLM call."""

    def available_tool_names(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
    ) -> frozenset[str]:
        del guild_id, channel_id, source_message_id
        return LIVE_BENCHMARK_FIXTURE_TOOL_NAMES

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions,
        run_id: str,
        step_index: int,
    ) -> ToolExecutionResult:
        del (
            guild_id,
            channel_id,
            source_message_id,
            permissions,
            run_id,
            step_index,
        )
        if tool_name == WEB_SEARCH_TOOL_NAME:
            return self._web(arguments)
        if tool_name == EXTRACT_URL_TOOL_NAME:
            return self._url_extract(arguments)
        if tool_name == BROWSER_EXTRACT_TOOL_NAME:
            return self._browser_extract(arguments)
        if tool_name == PYTHON_EXEC_TOOL_NAME:
            return self._python(arguments)
        if tool_name == STOCK_QUOTE_TOOL_NAME:
            return self._quote(arguments)
        if tool_name == PRICE_HISTORY_TOOL_NAME:
            return self._price_history(arguments)
        if tool_name == ANNUAL_PERFORMANCE_TOOL_NAME:
            return self._annual_performance(arguments)
        if tool_name == YOUTUBE_TRANSCRIPT_TOOL_NAME:
            return self._youtube_transcript(arguments)
        if tool_name == IMAGE_SEARCH_TOOL_NAME:
            return self._image_search(arguments)
        if tool_name == MEMORY_SEARCH_TOOL_NAME:
            return self._memory_search(arguments, requester_user_id=user_id)
        if tool_name == GET_CHANNEL_CONTEXT_TOOL_NAME:
            return self._channel_context(arguments)
        if tool_name == DEEP_RESEARCH_TOOL_NAME:
            return self._deep_research(arguments)
        return ToolExecutionResult(
            content=f"{tool_name} is unavailable in the live benchmark fixture.",
            status=ToolStatus.ERROR,
            metrics={"live_benchmark_unexpected_tool_count": 1},
        )

    @staticmethod
    def _web(arguments: str) -> ToolExecutionResult:
        return execute_fixture_web(arguments)

    @staticmethod
    def _url_extract(arguments: str) -> ToolExecutionResult:
        return execute_fixture_url_extract(arguments)

    @staticmethod
    def _browser_extract(arguments: str) -> ToolExecutionResult:
        return execute_fixture_browser_extract(arguments)

    @staticmethod
    def _python(arguments: str) -> ToolExecutionResult:
        return execute_fixture_python(arguments)

    @staticmethod
    def _quote(arguments: str) -> ToolExecutionResult:
        return execute_fixture_quote(arguments)

    @staticmethod
    def _price_history(arguments: str) -> ToolExecutionResult:
        return execute_fixture_price_history(arguments)

    @staticmethod
    def _annual_performance(arguments: str) -> ToolExecutionResult:
        return execute_fixture_annual_performance(arguments)

    @staticmethod
    def _youtube_transcript(arguments: str) -> ToolExecutionResult:
        return execute_fixture_youtube_transcript(arguments)

    @staticmethod
    def _image_search(arguments: str) -> ToolExecutionResult:
        return execute_fixture_image_search(arguments)

    @staticmethod
    def _memory_search(
        arguments: str,
        *,
        requester_user_id: int,
    ) -> ToolExecutionResult:
        return execute_fixture_memory_search(
            arguments,
            requester_user_id=requester_user_id,
        )

    @staticmethod
    def _channel_context(arguments: str) -> ToolExecutionResult:
        return execute_fixture_channel_context(arguments)

    @staticmethod
    def _deep_research(arguments: str) -> ToolExecutionResult:
        return execute_fixture_deep_research(arguments)


def build_live_benchmark_fixture_tool_runner() -> ToolRunner:
    return ToolRunner(LiveBenchmarkFixtureExecutor())

def _check(check_id: str, passed: bool, detail: str) -> LiveBenchmarkCheckResult:
    return LiveBenchmarkCheckResult(check_id=check_id, passed=passed, detail=detail)


def _has_deliverable_image(
    answer: str,
    *,
    metrics: Mapping[str, int | str],
) -> bool:
    trusted_urls = _successful_image_provenance(metrics)
    for match in _MARKDOWN_IMAGE_RE.finditer(answer):
        url = match.group("url").rstrip(".,;:!?\"'")
        if url in trusted_urls or _has_image_path_suffix(url):
            return True
    ordinary_link_spans = tuple(match.span() for match in _MARKDOWN_LINK_RE.finditer(answer))
    for match in _RAW_URL_RE.finditer(answer):
        if any(start <= match.start() < end for start, end in ordinary_link_spans):
            continue
        url = match.group(0).rstrip(".,;:!?\"'")
        line_start = answer.rfind("\n", 0, match.start()) + 1
        line_end = answer.find("\n", match.end())
        if line_end < 0:
            line_end = len(answer)
        prefix = answer[line_start:match.start()]
        suffix = answer[match.end():line_end].strip()
        if prefix.strip() or _EVIDENCE_MARKERS_ONLY_RE.fullmatch(suffix) is None:
            continue
        if url in trusted_urls or _has_image_path_suffix(url):
            return True
    return False


def _has_image_path_suffix(url: str) -> bool:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        return False
    return path.endswith(_IMAGE_PATH_SUFFIXES)


def _successful_image_provenance(
    metrics: Mapping[str, int | str],
) -> frozenset[str]:
    serialized = metrics.get("_diagnostic_agent_steps_json")
    if not isinstance(serialized, str) or not serialized.strip():
        return frozenset()
    try:
        steps = json.loads(serialized)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(steps, list):
        return frozenset()
    urls: set[str] = set()
    for step in steps:
        if (
            not isinstance(step, dict)
            or step.get("tool_name") != IMAGE_SEARCH_TOOL_NAME
            or str(step.get("status", "")).casefold() != "ok"
        ):
            continue
        details = step.get("details")
        provenance = details.get("provenance") if isinstance(details, dict) else None
        if isinstance(provenance, list):
            urls.update(str(value).strip() for value in provenance if str(value).strip())
    return frozenset(urls)


def _available_tools_for_case(
    resolver: AvailableToolResolver | None,
    case: LiveBenchmarkCase,
) -> frozenset[str] | None:
    if resolver is None:
        return None
    resolved = resolver(case) if callable(resolver) else resolver
    if resolved is None:
        return None
    return frozenset(resolved)


def _unavailable_reason(
    case: LiveBenchmarkCase,
    available_tools: frozenset[str] | None,
) -> str:
    if available_tools is None:
        return ""
    missing = case.checks.required_capabilities - available_tools
    if missing:
        return "required tools unavailable: " + ", ".join(sorted(missing))
    any_tools = frozenset(case.checks.required_any_tools)
    if any_tools and any_tools.isdisjoint(available_tools):
        return "no acceptable grounding tool is available: " + ", ".join(
            sorted(any_tools)
        )
    return ""


def _attempt_numeric_metrics(
    attempts: Collection[LiveBenchmarkAttempt],
    *metric_names: str,
) -> list[float]:
    values: list[float] = []
    for attempt in attempts:
        execution = attempt.execution
        if execution is None:
            continue
        for metric_name in metric_names:
            value = numeric_metric(execution.metrics.get(metric_name))
            if value is not None:
                values.append(value)
                break
    return values


def _average_int(values: Collection[int | float]) -> int:
    return round(statistics.fmean(values)) if values else 0


def _average_float(values: Collection[int | float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def _percentile(values: Collection[int | float], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(quantile * len(ordered)) - 1, 0)
    return round(ordered[index])


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _elapsed_ms(started_at: float) -> int:
    return max(round((time.perf_counter() - started_at) * 1000), 0)


async def _finish_observer_after_cancellation(
    observer_task: asyncio.Future[None],
    *,
    attempt_id: str,
) -> None:
    """Keep a completed attempt's save alive through repeated task cancellation."""
    while not observer_task.done():
        try:
            await asyncio.shield(observer_task)
        except asyncio.CancelledError:
            if observer_task.done():
                break
            continue
        except Exception:
            break
    if observer_task.cancelled():
        LOGGER.error(
            "Live benchmark persistence was cancelled while saving %s.",
            attempt_id,
        )
        return
    try:
        observer_task.result()
    except Exception:
        LOGGER.exception(
            "Live benchmark persistence failed while cancelling %s.",
            attempt_id,
        )
