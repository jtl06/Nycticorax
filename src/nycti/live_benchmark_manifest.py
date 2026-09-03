from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Mapping

from nycti.chat.tools.registry import TOOL_SPECS
from nycti.live_benchmark_regex import parse_regex_groups, parse_regex_list


_ROOT_KEYS = frozenset({"version", "description", "mode_defaults", "cases"})
_CASE_KEYS = frozenset(
    {
        "id",
        "mode",
        "prompt",
        "description",
        "context",
        "discord",
        "image_fixture",
        "checks",
    }
)
_CONTEXT_KEYS = frozenset(
    {"personal_profile", "memories", "memory_snapshot", "market_watchlist"}
)
_DISCORD_CONTEXT_KEYS = frozenset({"recent_messages", "reply_chain"})
_DISCORD_MESSAGE_KEYS = frozenset({"author", "content", "minutes_ago"})
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


def parse_live_benchmark_manifest(raw: object):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import LiveBenchmarkManifest

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
        _parse_case(value, index=index, mode_defaults=mode_defaults)
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


def _parse_case(value: object, *, index: int, mode_defaults: Mapping):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import (
        MAX_LIVE_BENCHMARK_PROMPT_CHARS,
        LiveBenchmarkCase,
        LiveBenchmarkMode,
    )

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
    context = _parse_prompt_context(raw.get("context"), case_id=case_id)
    discord_context = _parse_discord_context(raw.get("discord"), case_id=case_id)
    if mode is not LiveBenchmarkMode.FIXTURES and (
        not context.is_empty or not discord_context.is_empty
    ):
        raise ValueError(
            f"Live benchmark case {case_id} context is allowed only for fixture cases"
        )
    image_fixture = _optional_string(raw, "image_fixture")
    if image_fixture:
        image_path = Path(image_fixture)
        if (
            image_path.is_absolute()
            or ".." in image_path.parts
            or image_path.suffix.casefold() not in {".jpeg", ".jpg", ".png", ".webp"}
        ):
            raise ValueError(
                f"Live benchmark case {case_id} image_fixture must be a relative "
                "JPEG, PNG, or WebP path"
            )
    return LiveBenchmarkCase(
        case_id=case_id,
        mode=mode,
        prompt=prompt,
        checks=checks,
        description=_optional_string(raw, "description"),
        context=context,
        discord=discord_context,
        image_fixture=image_fixture,
    )


def _parse_prompt_context(value: object | None, *, case_id: str):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import (
        MAX_LIVE_BENCHMARK_CONTEXT_CHARS,
        LiveBenchmarkPromptContext,
    )

    if value is None:
        return LiveBenchmarkPromptContext()
    label = f"Live benchmark case {case_id} context"
    raw = _object(value, label)
    _reject_unknown_keys(raw, _CONTEXT_KEYS, label)
    fields = {
        name: _optional_string(raw, name)
        for name in ("personal_profile", "memories", "memory_snapshot", "market_watchlist")
    }
    for field_name, field_value in fields.items():
        if len(field_value) > MAX_LIVE_BENCHMARK_CONTEXT_CHARS:
            raise ValueError(
                f"Live benchmark case {case_id} context {field_name} exceeds "
                f"{MAX_LIVE_BENCHMARK_CONTEXT_CHARS} characters"
            )
    return LiveBenchmarkPromptContext(**fields)


def _parse_discord_context(value: object | None, *, case_id: str):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import (
        MAX_LIVE_BENCHMARK_DISCORD_MESSAGES,
        LiveBenchmarkDiscordContext,
    )

    if value is None:
        return LiveBenchmarkDiscordContext()
    label = f"Live benchmark case {case_id} discord"
    raw = _object(value, label)
    _reject_unknown_keys(raw, _DISCORD_CONTEXT_KEYS, label)
    recent_messages = _parse_discord_messages(
        raw.get("recent_messages", []), case_id=case_id, field_name="recent_messages"
    )
    reply_chain = _parse_discord_messages(
        raw.get("reply_chain", []), case_id=case_id, field_name="reply_chain"
    )
    if tuple(message.minutes_ago for message in recent_messages) != tuple(
        sorted((message.minutes_ago for message in recent_messages), reverse=True)
    ):
        raise ValueError(
            f"Live benchmark case {case_id} discord recent_messages must be ordered oldest to newest"
        )
    if tuple(message.minutes_ago for message in reply_chain) != tuple(
        sorted(message.minutes_ago for message in reply_chain)
    ):
        raise ValueError(
            f"Live benchmark case {case_id} discord reply_chain must be ordered nearest reply first"
        )
    if len(recent_messages) + len(reply_chain) > MAX_LIVE_BENCHMARK_DISCORD_MESSAGES:
        raise ValueError(
            f"Live benchmark case {case_id} discord context exceeds "
            f"{MAX_LIVE_BENCHMARK_DISCORD_MESSAGES} messages"
        )
    return LiveBenchmarkDiscordContext(
        recent_messages=recent_messages,
        reply_chain=reply_chain,
    )


def _parse_discord_messages(
    value: object,
    *,
    case_id: str,
    field_name: str,
):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import (
        MAX_LIVE_BENCHMARK_DISCORD_AGE_MINUTES,
        MAX_LIVE_BENCHMARK_DISCORD_MESSAGE_CHARS,
        LiveBenchmarkDiscordMessage,
    )

    if not isinstance(value, list):
        raise ValueError(
            f"Live benchmark case {case_id} discord {field_name} must be an array"
        )
    messages = []
    for index, item in enumerate(value):
        label = f"Live benchmark case {case_id} discord {field_name} message {index}"
        raw = _object(item, label)
        _reject_unknown_keys(raw, _DISCORD_MESSAGE_KEYS, label)
        author = _required_string(raw, "author", label)
        content = _required_string(raw, "content", label)
        minutes_ago = raw.get("minutes_ago")
        if (
            not isinstance(minutes_ago, int)
            or isinstance(minutes_ago, bool)
            or not 0 <= minutes_ago <= MAX_LIVE_BENCHMARK_DISCORD_AGE_MINUTES
        ):
            raise ValueError(
                f"{label} minutes_ago must be an integer between 0 and "
                f"{MAX_LIVE_BENCHMARK_DISCORD_AGE_MINUTES}"
            )
        if len(author) > 80:
            raise ValueError(f"{label} author must not exceed 80 characters")
        if len(content) > MAX_LIVE_BENCHMARK_DISCORD_MESSAGE_CHARS:
            raise ValueError(
                f"{label} content exceeds {MAX_LIVE_BENCHMARK_DISCORD_MESSAGE_CHARS} characters"
            )
        messages.append(
            LiveBenchmarkDiscordMessage(
                author=author,
                content=content,
                minutes_ago=minutes_ago,
            )
        )
    return tuple(messages)


def _parse_checks(value: object, *, case_id: str):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import LiveBenchmarkChecks

    label = f"Live benchmark case {case_id} checks"
    raw = _object(value, label)
    _reject_unknown_keys(raw, _CHECK_KEYS, label)
    required_tools = _tool_names(raw, "required_tools", case_id=case_id)
    required_attempted_tools = _tool_names(
        raw, "required_attempted_tools", case_id=case_id
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
    grounding_required = raw.get("grounding_required", False)
    image_delivery_required = raw.get("image_delivery_required", False)
    if not isinstance(grounding_required, bool):
        raise ValueError(
            f"Live benchmark case {case_id} grounding_required must be a boolean"
        )
    if not isinstance(image_delivery_required, bool):
        raise ValueError(
            f"Live benchmark case {case_id} image_delivery_required must be a boolean"
        )
    return LiveBenchmarkChecks(
        required_tools=required_tools,
        required_attempted_tools=required_attempted_tools,
        required_any_tools=required_any_tools,
        forbidden_tools=forbidden_tools,
        answer_regex=parse_regex_list(raw, "answer_regex", case_id=case_id),
        answer_regex_groups=parse_regex_groups(raw, case_id=case_id),
        forbidden_answer_regex=parse_regex_list(
            raw, "forbidden_answer_regex", case_id=case_id
        ),
        metric_min=_metric_min(raw, case_id=case_id),
        metric_max=_metric_max(raw, case_id=case_id),
        metric_equals=_metric_equals(raw, case_id=case_id),
        grounding_required=grounding_required,
        image_delivery_required=image_delivery_required,
        max_answer_chars=_optional_positive_int(raw, "max_answer_chars", case_id=case_id),
        max_tool_calls=_optional_nonnegative_int(raw, "max_tool_calls", case_id=case_id),
    )


def _parse_mode_defaults(value: object):  # type: ignore[no-untyped-def]
    from nycti.live_benchmarks import LiveBenchmarkMode

    raw = _object(value, "Live benchmark mode_defaults")
    _reject_unknown_keys(raw, _MODE_DEFAULT_KEYS, "Live benchmark mode_defaults")
    parsed = {}
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


def _merge_mode_default_checks(defaults, case_checks):  # type: ignore[no-untyped-def]
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
    value: Mapping[str, object], allowed: frozenset[str], label: str
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
    return _numeric_metric_map(value, "metric_min", case_id=case_id)


def _metric_max(value: Mapping[str, object], *, case_id: str) -> dict[str, float]:
    return _numeric_metric_map(value, "metric_max", case_id=case_id)


def _numeric_metric_map(
    value: Mapping[str, object], key: str, *, case_id: str
) -> dict[str, float]:
    item = value.get(key, {})
    if not isinstance(item, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(number, (int, float))
        and not isinstance(number, bool)
        for name, number in item.items()
    ):
        raise ValueError(
            f"Live benchmark case {case_id} {key} must map metric names to numbers"
        )
    return {name: float(number) for name, number in item.items()}


def _metric_equals(
    value: Mapping[str, object], *, case_id: str
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
    value: Mapping[str, object], key: str, *, case_id: str
) -> int | None:
    return _optional_bounded_int(value, key, case_id=case_id, minimum=1)


def _optional_nonnegative_int(
    value: Mapping[str, object], key: str, *, case_id: str
) -> int | None:
    return _optional_bounded_int(value, key, case_id=case_id, minimum=0)


def _optional_bounded_int(
    value: Mapping[str, object], key: str, *, case_id: str, minimum: int
) -> int | None:
    if key not in value:
        return None
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(
            f"Live benchmark case {case_id} {key} must be a {qualifier} integer"
        )
    return item
