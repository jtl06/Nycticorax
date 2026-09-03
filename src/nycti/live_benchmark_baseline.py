from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Mapping

from nycti.live_benchmarks import (
    LiveBenchmarkAttempt,
    LiveBenchmarkStatus,
    LiveBenchmarkSuiteResult,
    aggregate_live_benchmark_suite,
)

LIVE_BENCHMARK_BASELINE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LiveBenchmarkBaselineComparison:
    passed: bool
    failures: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()


def build_live_benchmark_baseline(
    result: LiveBenchmarkSuiteResult,
) -> dict[str, object]:
    aggregate = aggregate_live_benchmark_suite(result)
    case_ids = tuple(sorted({attempt.case.case_id for attempt in result.attempts}))
    return {
        "schema_version": LIVE_BENCHMARK_BASELINE_SCHEMA_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest_version": result.manifest_version,
        "mode": result.mode.value,
        "case_ids": list(case_ids),
        "models": sorted(_observed_models(result.attempts)),
        "aggregate": asdict(aggregate),
        "cases": {
            case_id: _case_baseline(result.attempts, case_id=case_id)
            for case_id in case_ids
        },
    }


def write_live_benchmark_baseline(
    path: str | Path,
    result: LiveBenchmarkSuiteResult,
) -> None:
    Path(path).write_text(
        json.dumps(
            build_live_benchmark_baseline(result),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def load_live_benchmark_baseline(path: str | Path) -> Mapping[str, object]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Live benchmark baseline must be a JSON object")
    return raw


def compare_live_benchmark_baseline(
    result: LiveBenchmarkSuiteResult,
    baseline: Mapping[str, object],
    *,
    latency_tolerance: float = 0.15,
    latency_floor_ms: int = 250,
    quality_tolerance: float = 0.005,
) -> LiveBenchmarkBaselineComparison:
    if not 0 <= latency_tolerance <= 2:
        raise ValueError("latency_tolerance must be between 0 and 2")
    failures: list[str] = []
    notices: list[str] = []
    if baseline.get("schema_version") != LIVE_BENCHMARK_BASELINE_SCHEMA_VERSION:
        failures.append("baseline schema version does not match")
    if baseline.get("manifest_version") != result.manifest_version:
        failures.append(
            f"manifest changed: baseline={baseline.get('manifest_version')!r}, "
            f"current={result.manifest_version}"
        )
    if baseline.get("mode") != result.mode.value:
        failures.append(
            f"mode changed: baseline={baseline.get('mode')!r}, current={result.mode.value!r}"
        )

    current_case_ids = sorted({attempt.case.case_id for attempt in result.attempts})
    baseline_case_ids = baseline.get("case_ids")
    if baseline_case_ids != current_case_ids:
        failures.append("selected benchmark cases differ from the baseline")

    baseline_aggregate = baseline.get("aggregate")
    if not isinstance(baseline_aggregate, dict):
        failures.append("baseline aggregate is missing or invalid")
        return LiveBenchmarkBaselineComparison(False, tuple(failures), tuple(notices))
    current = asdict(aggregate_live_benchmark_suite(result))
    for count_name in ("fail_count", "error_count"):
        baseline_count = _number(baseline_aggregate.get(count_name))
        if baseline_count is None:
            failures.append(f"baseline aggregate lacks {count_name}")
        elif current[count_name] > baseline_count:
            failures.append(
                f"{count_name} regressed: baseline={baseline_count:g}, "
                f"current={current[count_name]}"
            )
    for rate_name in ("pass_rate", "check_rate"):
        baseline_rate = _number(baseline_aggregate.get(rate_name))
        if baseline_rate is None:
            failures.append(f"baseline aggregate lacks {rate_name}")
        elif current[rate_name] + quality_tolerance < baseline_rate:
            failures.append(
                f"{rate_name} regressed: baseline={baseline_rate:.1%}, "
                f"current={current[rate_name]:.1%}"
            )
    baseline_samples = int(_number(baseline_aggregate.get("attempt_count")) or 0)
    current_samples = int(_number(current.get("attempt_count")) or 0)
    latency_names: tuple[str, ...] = ()
    if min(baseline_samples, current_samples) >= 3:
        latency_names = ("latency_p50_ms",)
    else:
        notices.append("median latency comparison skipped: fewer than 3 attempts")
    if min(baseline_samples, current_samples) >= 10:
        latency_names = (*latency_names, "latency_p90_ms")
    else:
        notices.append("p90 latency comparison skipped: fewer than 10 attempts")
    for latency_name in latency_names:
        baseline_latency = _number(baseline_aggregate.get(latency_name))
        if baseline_latency is None:
            failures.append(f"baseline aggregate lacks {latency_name}")
            continue
        if baseline_latency <= 0:
            continue
        allowed = max(
            baseline_latency * (1 + latency_tolerance),
            baseline_latency + latency_floor_ms,
        )
        if current[latency_name] > allowed:
            failures.append(
                f"{latency_name} regressed: baseline={baseline_latency:.0f}ms, "
                f"current={current[latency_name]}ms, allowed={allowed:.0f}ms"
            )

    baseline_cases = baseline.get("cases")
    if isinstance(baseline_cases, dict):
        for case_id, baseline_case in baseline_cases.items():
            if not isinstance(case_id, str) or not isinstance(baseline_case, dict):
                continue
            if baseline_case.get("passed") is not True:
                continue
            current_case = _case_baseline(result.attempts, case_id=case_id)
            if current_case["passed"] is not True:
                failures.append(f"previously passing case regressed: {case_id}")

    return LiveBenchmarkBaselineComparison(not failures, tuple(failures), tuple(notices))


def _case_baseline(
    attempts: tuple[LiveBenchmarkAttempt, ...],
    *,
    case_id: str,
) -> dict[str, object]:
    selected = tuple(attempt for attempt in attempts if attempt.case.case_id == case_id)
    active = tuple(
        attempt for attempt in selected if attempt.status != LiveBenchmarkStatus.SKIP
    )
    return {
        "attempt_count": len(active),
        "passed": bool(active)
        and all(attempt.status == LiveBenchmarkStatus.PASS for attempt in active),
        "statuses": [attempt.status.value for attempt in active],
        "latency_avg_ms": (
            round(sum(attempt.latency_ms for attempt in active) / len(active))
            if active
            else 0
        ),
    }


def _observed_models(attempts: tuple[LiveBenchmarkAttempt, ...]) -> set[str]:
    models: set[str] = set()
    for attempt in attempts:
        execution = attempt.execution
        if execution is None:
            continue
        for key in ("active_chat_model", "chat_model"):
            value = execution.metrics.get(key)
            if isinstance(value, str) and value.strip():
                models.add(value.strip())
                break
    return models


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
