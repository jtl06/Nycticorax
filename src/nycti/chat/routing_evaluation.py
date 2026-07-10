from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any, Sequence

from nycti.chat.evidence import build_evidence_ledger
from nycti.chat.run_state import AnswerProfile
from nycti.chat.tool_eligibility import READ_ONLY_TOOL_NAMES, select_answer_plan

if TYPE_CHECKING:
    from nycti.chat.run_state import AgentRun

DEFAULT_ROUTING_CORPUS = Path(__file__).resolve().parents[3] / "benchmarks" / "routing_cases.json"
GROUNDING_QUALITY_PASS_SCORE = 0.8

# Stable names for dashboards and offline observation exports.  The live path
# records what it can know cheaply; answer-quality scores can also be supplied
# by a human or judge when replaying the labeled corpus.
ROUTING_TELEMETRY_SCHEMA: dict[str, str] = {
    "routing_exposed_tools": "Comma-separated direct tool schemas shown to the model.",
    "routing_exposed_tool_count": "Number of direct tool schemas shown to the model.",
    "routing_deferred_tools": "Comma-separated tools available through a future resolver.",
    "routing_promoted_tools": "Comma-separated nonbinding relevance hints.",
    "routing_unavailable_promoted_tools": "Promoted tools unavailable in this runtime.",
    "routing_called_tools": "Comma-separated tools attempted by the model.",
    "routing_called_tool_count": "Number of distinct tools attempted by the model.",
    "routing_exposure_miss_count": "Promoted tools that were not reachable.",
    "routing_tool_call_miss_count": "One when tools were promoted but none were called.",
    "routing_grounding_expected": "One when this route is expected to use grounding evidence.",
    "routing_grounding_miss_count": "One when expected grounding produced no valid evidence.",
    "routing_latency_ms": "End-to-end agent latency in milliseconds.",
    "routing_grounding_quality_score": (
        "0-100 citation-contract score for externally grounded answers, or unscored."
    ),
}


@dataclass(frozen=True, slots=True)
class RoutingCase:
    case_id: str
    prompt: str
    category: str
    grounding_required: bool
    required_exposed_tools: frozenset[str]
    acceptable_called_tools: frozenset[str]
    expected_promoted_tools: frozenset[str]


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    case_id: str
    exposed_tool_names: frozenset[str]
    promoted_tool_names: frozenset[str]
    called_tool_names: frozenset[str] | None = None
    latency_ms: int | None = None
    grounding_quality_score: float | None = None


@dataclass(frozen=True, slots=True)
class RoutingEvaluationMetrics:
    case_count: int
    exposure_miss_count: int
    promotion_miss_count: int
    unexpected_promotion_count: int
    observed_call_count: int
    call_miss_count: int
    observed_latency_count: int
    mean_latency_ms: float | None
    observed_quality_count: int
    mean_grounding_quality: float | None
    grounding_quality_pass_rate: float | None

    def as_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


def load_routing_corpus(path: Path = DEFAULT_ROUTING_CORPUS) -> tuple[RoutingCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError("Routing corpus must contain a cases array")
    cases = tuple(_parse_case(value) for value in raw["cases"])
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Routing corpus case IDs must be unique")
    return cases


def observe_static_routes(
    cases: Sequence[RoutingCase],
    *,
    guild_id: int | None = 1,
) -> tuple[RoutingObservation, ...]:
    observations: list[RoutingObservation] = []
    for case in cases:
        plan, _ = select_answer_plan(request_text=case.prompt, guild_id=guild_id)
        observations.append(
            RoutingObservation(
                case_id=case.case_id,
                exposed_tool_names=plan.direct_tool_names,
                promoted_tool_names=frozenset(plan.promoted_tool_names),
            )
        )
    return tuple(observations)


def evaluate_routing(
    cases: Sequence[RoutingCase],
    observations: Sequence[RoutingObservation],
) -> RoutingEvaluationMetrics:
    by_id = {observation.case_id: observation for observation in observations}
    if len(by_id) != len(observations):
        raise ValueError("Routing observations must have unique case IDs")
    unknown_ids = set(by_id) - {case.case_id for case in cases}
    if unknown_ids:
        raise ValueError(f"Unknown routing observation IDs: {sorted(unknown_ids)}")

    exposure_misses = 0
    promotion_misses = 0
    unexpected_promotions = 0
    observed_calls = 0
    call_misses = 0
    latencies: list[int] = []
    quality_scores: list[float] = []
    quality_passes = 0

    for case in cases:
        observation = by_id.get(case.case_id)
        if observation is None:
            exposure_misses += 1
            continue
        if not case.required_exposed_tools <= observation.exposed_tool_names:
            exposure_misses += 1
        if not case.expected_promoted_tools <= observation.promoted_tool_names:
            promotion_misses += 1
        if not case.grounding_required and observation.promoted_tool_names:
            unexpected_promotions += 1
        if observation.called_tool_names is not None:
            observed_calls += 1
            if (
                case.grounding_required
                and case.acceptable_called_tools
                and not (case.acceptable_called_tools & observation.called_tool_names)
            ):
                call_misses += 1
        if observation.latency_ms is not None:
            latencies.append(max(observation.latency_ms, 0))
        if observation.grounding_quality_score is not None:
            score = observation.grounding_quality_score
            if not 0.0 <= score <= 1.0:
                raise ValueError("Grounding quality scores must be between 0 and 1")
            quality_scores.append(score)
            quality_passes += int(score >= GROUNDING_QUALITY_PASS_SCORE)

    return RoutingEvaluationMetrics(
        case_count=len(cases),
        exposure_miss_count=exposure_misses,
        promotion_miss_count=promotion_misses,
        unexpected_promotion_count=unexpected_promotions,
        observed_call_count=observed_calls,
        call_miss_count=call_misses,
        observed_latency_count=len(latencies),
        mean_latency_ms=fmean(latencies) if latencies else None,
        observed_quality_count=len(quality_scores),
        mean_grounding_quality=fmean(quality_scores) if quality_scores else None,
        grounding_quality_pass_rate=(
            quality_passes / len(quality_scores) if quality_scores else None
        ),
    )


def record_runtime_routing_metrics(
    metrics: dict[str, int | str] | None,
    *,
    run: AgentRun,
    answer_text: str,
) -> None:
    if metrics is None or run.answer_plan is None:
        return
    plan = run.answer_plan
    exposed = plan.direct_tool_names
    available_promoted = frozenset(plan.promoted_tool_names)
    unavailable_promoted = frozenset(plan.unavailable_promoted_tool_names)
    promoted = available_promoted | unavailable_promoted
    called = frozenset(run.attempted_tools)
    successful = frozenset(run.successful_tools)
    grounding_expected = _runtime_grounding_expected(
        metrics.get("routing_grounding_expected"),
        profile=plan.profile,
        promoted=promoted,
        called=called,
    )
    metrics["routing_exposed_tools"] = ", ".join(sorted(exposed)) or "(none)"
    metrics["routing_exposed_tool_count"] = len(exposed)
    metrics["routing_deferred_tools"] = (
        ", ".join(sorted(plan.deferred_tool_names)) or "(none)"
    )
    metrics["routing_promoted_tools"] = ", ".join(plan.promoted_tool_names) or "(none)"
    metrics["routing_unavailable_promoted_tools"] = (
        ", ".join(plan.unavailable_promoted_tool_names) or "(none)"
    )
    metrics["routing_called_tools"] = ", ".join(sorted(called)) or "(none)"
    metrics["routing_called_tool_count"] = len(called)
    metrics["routing_successful_tools"] = ", ".join(sorted(successful)) or "(none)"
    metrics["routing_successful_tool_count"] = len(successful)
    metrics["routing_exposure_miss_count"] = len(promoted - plan.reachable_tool_names)
    metrics["routing_tool_call_miss_count"] = int(
        bool(promoted and promoted.isdisjoint(called))
    )
    metrics["routing_latency_ms"] = run.elapsed_ms()
    metrics["routing_grounding_expected"] = int(grounding_expected)
    quality = _runtime_grounding_quality(
        run,
        answer_text,
        grounding_expected=grounding_expected,
    )
    metrics["routing_grounding_quality_score"] = quality
    metrics["routing_grounding_miss_count"] = int(grounding_expected and quality == 0)


def _runtime_grounding_expected(
    override: int | str | None,
    *,
    profile: AnswerProfile,
    promoted: frozenset[str],
    called: frozenset[str],
) -> bool:
    if isinstance(override, int):
        return bool(override)
    if isinstance(override, str) and override.casefold() in {"yes", "true", "1"}:
        return True
    if isinstance(override, str) and override.casefold() in {"no", "false", "0"}:
        return False
    return (
        profile == AnswerProfile.DEEP
        or bool(promoted)
        or bool(called.intersection(READ_ONLY_TOOL_NAMES))
    )


def _runtime_grounding_quality(
    run: AgentRun,
    answer_text: str,
    *,
    grounding_expected: bool,
) -> int | str:
    ledger = build_evidence_ledger(run.outcomes)
    if not ledger.items:
        return 0 if grounding_expected else "unscored"
    require_citations = ledger.researched or bool(
        run.answer_plan is not None
        and run.answer_plan.profile == AnswerProfile.DEEP
    )
    audit = ledger.audit_answer(answer_text, researched=require_citations)
    return 100 if audit.valid else 0


def _parse_case(value: object) -> RoutingCase:
    if not isinstance(value, dict):
        raise ValueError("Each routing case must be an object")
    case_id = _required_string(value, "id")
    prompt = _required_string(value, "prompt")
    category = _required_string(value, "category")
    grounding_required = value.get("grounding_required")
    if not isinstance(grounding_required, bool):
        raise ValueError(f"Routing case {case_id} must declare grounding_required")
    return RoutingCase(
        case_id=case_id,
        prompt=prompt,
        category=category,
        grounding_required=grounding_required,
        required_exposed_tools=_string_set(value, "required_exposed_tools"),
        acceptable_called_tools=_string_set(value, "acceptable_called_tools"),
        expected_promoted_tools=_string_set(value, "expected_promoted_tools"),
    )


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"Routing case field {key} must be a non-empty string")
    return item.strip()


def _string_set(value: dict[str, Any], key: str) -> frozenset[str]:
    items = value.get(key, [])
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError(f"Routing case field {key} must be an array of strings")
    return frozenset(item for item in items if item)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Nycti's labeled routing corpus")
    parser.add_argument("corpus", nargs="?", type=Path, default=DEFAULT_ROUTING_CORPUS)
    args = parser.parse_args(argv)
    cases = load_routing_corpus(args.corpus)
    result = evaluate_routing(cases, observe_static_routes(cases))
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return int(result.exposure_miss_count > 0)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
