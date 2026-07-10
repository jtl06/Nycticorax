from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

_NONE_METRIC_VALUES = frozenset({"", "(none)", "none", "null", "n/a"})


def extract_called_tools(metrics: Mapping[str, int | str]) -> tuple[str, ...]:
    called = metrics.get("routing_called_tools")
    if isinstance(called, str) and called.strip().casefold() not in _NONE_METRIC_VALUES:
        return tuple(
            value
            for value in (part.strip() for part in called.split(","))
            if value
        )

    serialized = metrics.get("_diagnostic_agent_messages_json")
    if not isinstance(serialized, str) or not serialized.strip():
        return ()
    try:
        messages = json.loads(serialized)
    except json.JSONDecodeError:
        return ()
    if not isinstance(messages, list):
        return ()
    names: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return tuple(names)


def extract_successful_tools(metrics: Mapping[str, int | str]) -> tuple[str, ...]:
    successful = metrics.get("routing_successful_tools")
    if (
        isinstance(successful, str)
        and successful.strip().casefold() not in _NONE_METRIC_VALUES
    ):
        return tuple(
            value
            for value in (part.strip() for part in successful.split(","))
            if value
        )

    serialized = metrics.get("_diagnostic_agent_steps_json")
    if not isinstance(serialized, str) or not serialized.strip():
        return ()
    try:
        steps = json.loads(serialized)
    except json.JSONDecodeError:
        return ()
    if not isinstance(steps, list):
        return ()
    return tuple(
        str(step["tool_name"])
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("tool_name"), str)
        and str(step.get("status", "")).casefold() == "ok"
    )


def grounding_passed(metrics: Mapping[str, int | str]) -> bool:
    quality = _numeric_metric(metrics.get("routing_grounding_quality_score"))
    if quality is not None:
        return quality >= 100
    evidence_count = _numeric_metric(metrics.get("evidence_item_count")) or 0
    if evidence_count < 1:
        return False
    failure_metrics = (
        "evidence_audit_failure_count",
        "evidence_unknown_citation_count",
        "evidence_unprovenanced_url_count",
        "evidence_missing_citation_count",
    )
    return all(
        (_numeric_metric(metrics.get(name)) or 0) == 0
        for name in failure_metrics
    )


def observed_tool_call_count(
    metrics: Mapping[str, int | str],
    called_tools: Sequence[str],
) -> int:
    for name in ("agent_tool_call_count", "tool_call_count"):
        value = _numeric_metric(metrics.get(name))
        if value is not None and value >= 0:
            return int(value)
    return len(called_tools)


def infrastructure_error(
    *,
    error: str,
    metrics: Mapping[str, int | str],
) -> str:
    if error.strip():
        return error.strip()
    stop_reason = str(metrics.get("agent_stop_reason", "")).strip().casefold()
    final_status = str(metrics.get("agent_final_status", "")).strip().casefold()
    model_turns = _numeric_metric(metrics.get("agent_model_turn_count"))
    provider_errors = _numeric_metric(metrics.get("agent_provider_error_count")) or 0
    timeouts = _numeric_metric(metrics.get("tool_timeout_count")) or 0
    if timeouts > 0 and not extract_successful_tools(metrics):
        return f"tool infrastructure timed out ({int(timeouts)} call(s))"
    if stop_reason == "provider_error" and (
        model_turns is None or model_turns == 0 or final_status == "fallback"
    ):
        return "foreground model provider failed before a usable answer"
    if provider_errors > 0 and model_turns == 0:
        return "foreground model provider failed before the first usable turn"
    failure_reason = str(metrics.get("agent_final_failure_reason", "")).strip()
    if final_status == "fallback" and failure_reason:
        return f"agent run ended in infrastructure fallback: {failure_reason}"
    provider_status_metrics = {
        "stock_quote_status": {"missing_key", "data_error", "http_error"},
        "price_history_status": {"missing_key", "data_error", "http_error"},
    }
    for metric_name, error_values in provider_status_metrics.items():
        observed = str(metrics.get(metric_name, "")).strip().casefold()
        if observed in error_values:
            return f"tool provider failed ({metric_name}={observed})"
    deep_status = str(metrics.get("deep_research_status", "")).strip().casefold()
    deep_sources = _numeric_metric(metrics.get("deep_research_source_count")) or 0
    if deep_status == "error" and deep_sources == 0:
        return "deep-research providers returned no usable evidence"
    return ""


def numeric_metric(value: object) -> float | None:
    return _numeric_metric(value)


def _numeric_metric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
