from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
from pathlib import Path
import re
import sysconfig
import time
from typing import Any, TypeAlias
from urllib.parse import urlsplit
from uuid import uuid4

from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tools.registry import TOOL_SPECS
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
from nycti.live_benchmark_regex import LiveBenchmarkRegexGroup, parse_regex_groups, parse_regex_list
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
MAX_LIVE_BENCHMARK_REPEATS = 3
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

_ROOT_KEYS = frozenset({"version", "description", "mode_defaults", "cases"})
_CASE_KEYS = frozenset({"id", "mode", "prompt", "description", "checks"})
_CHECK_KEYS = frozenset(
    {
        "required_tools",
        "required_attempted_tools",
        "required_any_tools",
        "forbidden_tools",
        "answer_regex",
        "answer_regex_groups",
        "forbidden_answer_regex",
        "metric_min",
        "metric_max",
        "metric_equals",
        "grounding_required",
        "image_delivery_required",
        "max_answer_chars",
        "max_tool_calls",
    }
)
_MODE_DEFAULT_KEYS = frozenset({"fixtures", "canaries"})
_MODE_DEFAULT_CHECK_KEYS = frozenset({"metric_max", "metric_equals"})
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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
class LiveBenchmarkCase:
    case_id: str
    mode: LiveBenchmarkMode
    prompt: str
    checks: LiveBenchmarkChecks
    description: str = ""


@dataclass(frozen=True, slots=True)
class LiveBenchmarkManifest:
    version: int
    cases: tuple[LiveBenchmarkCase, ...]
    description: str = ""
    mode_defaults: Mapping[LiveBenchmarkMode, LiveBenchmarkChecks] = field(default_factory=dict)

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
    return parse_live_benchmark_manifest(raw)


def parse_live_benchmark_manifest(raw: object) -> LiveBenchmarkManifest:
    root = _object(raw, "Live benchmark manifest")
    _reject_unknown_keys(root, _ROOT_KEYS, "Live benchmark manifest")
    version = root.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Live benchmark manifest version must be a positive integer")
    description = _optional_string(root, "description")
    raw_cases = root.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Live benchmark manifest cases must be a non-empty array")

    mode_defaults = _parse_mode_defaults(root.get("mode_defaults", {}))
    cases = tuple(
        _parse_case(
            value,
            index=index,
            mode_defaults=mode_defaults,
        )
        for index, value in enumerate(raw_cases)
    )
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"Duplicate live benchmark case id: {case.case_id}")
        seen.add(case.case_id)
    return LiveBenchmarkManifest(
        version=version,
        description=description,
        cases=cases,
        mode_defaults=mode_defaults,
    )


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


def _parse_case(
    value: object,
    *,
    index: int,
    mode_defaults: Mapping[LiveBenchmarkMode, LiveBenchmarkChecks],
) -> LiveBenchmarkCase:
    label = f"Live benchmark case at index {index}"
    raw = _object(value, label)
    _reject_unknown_keys(raw, _CASE_KEYS, label)
    case_id = _required_string(raw, "id", label)
    if _CASE_ID_RE.fullmatch(case_id) is None:
        raise ValueError(
            f"{label} id must use 1-64 lowercase letters, numbers, hyphens, or underscores"
        )
    prompt = _required_string(raw, "prompt", f"Live benchmark case {case_id}")
    if len(prompt) > MAX_LIVE_BENCHMARK_PROMPT_CHARS:
        raise ValueError(
            f"Live benchmark case {case_id} prompt is {len(prompt)} characters; "
            f"maximum is {MAX_LIVE_BENCHMARK_PROMPT_CHARS}"
        )
    if prompt != prompt.strip():
        raise ValueError(f"Live benchmark case {case_id} prompt must not have outer whitespace")
    raw_mode = _required_string(raw, "mode", f"Live benchmark case {case_id}")
    try:
        mode = LiveBenchmarkMode(raw_mode)
    except ValueError as exc:
        raise ValueError(
            f"Live benchmark case {case_id} mode must be fixtures or canaries"
        ) from exc
    if mode == LiveBenchmarkMode.ALL:
        raise ValueError(f"Live benchmark case {case_id} cannot use mode all")
    checks = _merge_mode_default_checks(
        mode_defaults.get(mode),
        _parse_checks(raw.get("checks"), case_id=case_id),
    )
    return LiveBenchmarkCase(
        case_id=case_id,
        mode=mode,
        prompt=prompt,
        checks=checks,
        description=_optional_string(raw, "description"),
    )


def _parse_checks(value: object, *, case_id: str) -> LiveBenchmarkChecks:
    label = f"Live benchmark case {case_id} checks"
    raw = _object(value, label)
    _reject_unknown_keys(raw, _CHECK_KEYS, label)
    required_tools = _tool_names(raw, "required_tools", case_id=case_id)
    required_attempted_tools = _tool_names(
        raw,
        "required_attempted_tools",
        case_id=case_id,
    )
    required_any_tools = _tool_names(
        raw,
        "required_any_tools",
        case_id=case_id,
        require_nonempty_if_present=True,
    )
    forbidden_tools = _tool_names(raw, "forbidden_tools", case_id=case_id)
    overlap = (
        set(required_tools) | set(required_attempted_tools) | set(required_any_tools)
    ).intersection(forbidden_tools)
    if overlap:
        raise ValueError(
            f"Live benchmark case {case_id} both requires and forbids tools: {sorted(overlap)}"
        )
    answer_regex = parse_regex_list(raw, "answer_regex", case_id=case_id)
    answer_regex_groups = parse_regex_groups(raw, case_id=case_id)
    forbidden_answer_regex = parse_regex_list(
        raw,
        "forbidden_answer_regex",
        case_id=case_id,
    )
    grounding_required = raw.get("grounding_required", False)
    if not isinstance(grounding_required, bool):
        raise ValueError(
            f"Live benchmark case {case_id} grounding_required must be a boolean"
        )
    image_delivery_required = raw.get("image_delivery_required", False)
    if not isinstance(image_delivery_required, bool):
        raise ValueError(
            f"Live benchmark case {case_id} image_delivery_required must be a boolean"
        )
    max_answer_chars = _optional_positive_int(
        raw,
        "max_answer_chars",
        case_id=case_id,
    )
    max_tool_calls = _optional_nonnegative_int(
        raw,
        "max_tool_calls",
        case_id=case_id,
    )
    return LiveBenchmarkChecks(
        required_tools=required_tools,
        required_attempted_tools=required_attempted_tools,
        required_any_tools=required_any_tools,
        forbidden_tools=forbidden_tools,
        answer_regex=answer_regex,
        answer_regex_groups=answer_regex_groups,
        forbidden_answer_regex=forbidden_answer_regex,
        metric_min=_metric_min(raw, case_id=case_id),
        metric_max=_metric_max(raw, case_id=case_id),
        metric_equals=_metric_equals(raw, case_id=case_id),
        grounding_required=grounding_required,
        image_delivery_required=image_delivery_required,
        max_answer_chars=max_answer_chars,
        max_tool_calls=max_tool_calls,
    )


def _parse_mode_defaults(
    value: object,
) -> dict[LiveBenchmarkMode, LiveBenchmarkChecks]:
    raw = _object(value, "Live benchmark mode_defaults")
    _reject_unknown_keys(raw, _MODE_DEFAULT_KEYS, "Live benchmark mode_defaults")
    parsed: dict[LiveBenchmarkMode, LiveBenchmarkChecks] = {}
    for mode_name, defaults_value in raw.items():
        label = f"Live benchmark mode_defaults {mode_name}"
        defaults_raw = _object(defaults_value, label)
        _reject_unknown_keys(defaults_raw, _MODE_DEFAULT_CHECK_KEYS, label)
        mode = LiveBenchmarkMode(mode_name)
        parsed[mode] = _parse_checks(
            defaults_raw,
            case_id=f"mode-default-{mode_name}",
        )
    return parsed


def _merge_mode_default_checks(
    defaults: LiveBenchmarkChecks | None,
    case_checks: LiveBenchmarkChecks,
) -> LiveBenchmarkChecks:
    if defaults is None:
        return case_checks
    return replace(
        case_checks,
        metric_max={**defaults.metric_max, **case_checks.metric_max},
        metric_equals={**defaults.metric_equals, **case_checks.metric_equals},
    )


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {unknown}")


def _required_string(value: Mapping[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{label} {key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item.strip()


def _tool_names(
    value: Mapping[str, object],
    key: str,
    *,
    case_id: str,
    require_nonempty_if_present: bool = False,
) -> tuple[str, ...]:
    item = value.get(key, [])
    if not isinstance(item, list) or not all(
        isinstance(name, str) and name for name in item
    ):
        raise ValueError(f"Live benchmark case {case_id} {key} must be a string array")
    if key in value and require_nonempty_if_present and not item:
        raise ValueError(f"Live benchmark case {case_id} {key} must not be empty")
    names = tuple(dict.fromkeys(item))
    unknown = sorted(set(names) - TOOL_SPECS.keys())
    if unknown:
        raise ValueError(
            f"Live benchmark case {case_id} {key} has unknown tools: {unknown}"
        )
    return names


def _metric_min(value: Mapping[str, object], *, case_id: str) -> dict[str, float]:
    item = value.get("metric_min", {})
    if not isinstance(item, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        for name, minimum in item.items()
    ):
        raise ValueError(
            f"Live benchmark case {case_id} metric_min must map metric names to numbers"
        )
    return {name: float(minimum) for name, minimum in item.items()}


def _metric_max(value: Mapping[str, object], *, case_id: str) -> dict[str, float]:
    item = value.get("metric_max", {})
    if not isinstance(item, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        for name, maximum in item.items()
    ):
        raise ValueError(
            f"Live benchmark case {case_id} metric_max must map metric names to numbers"
        )
    return {name: float(maximum) for name, maximum in item.items()}


def _metric_equals(
    value: Mapping[str, object],
    *,
    case_id: str,
) -> dict[str, int | str]:
    item = value.get("metric_equals", {})
    if not isinstance(item, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(expected, (int, str))
        and not isinstance(expected, bool)
        for name, expected in item.items()
    ):
        raise ValueError(
            f"Live benchmark case {case_id} metric_equals must map metric names to strings or integers"
        )
    return dict(item)


def _optional_positive_int(
    value: Mapping[str, object],
    key: str,
    *,
    case_id: str,
) -> int | None:
    if key not in value:
        return None
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"Live benchmark case {case_id} {key} must be a positive integer")
    return item


def _optional_nonnegative_int(
    value: Mapping[str, object],
    key: str,
    *,
    case_id: str,
) -> int | None:
    if key not in value:
        return None
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValueError(
            f"Live benchmark case {case_id} {key} must be a non-negative integer"
        )
    return item


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
        if answer[line_start:line_end].strip() != url:
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
