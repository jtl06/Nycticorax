from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re


_REGEX_GROUP_KEYS = frozenset({"patterns", "minimum", "case_sensitive"})


@dataclass(frozen=True, slots=True)
class LiveBenchmarkRegexGroup:
    patterns: tuple[str, ...]
    minimum: int
    case_sensitive: bool = False


def parse_regex_list(
    value: Mapping[str, object],
    key: str,
    *,
    case_id: str,
) -> tuple[str, ...]:
    item = value.get(key, [])
    if not isinstance(item, list) or not all(
        isinstance(pattern, str) and pattern for pattern in item
    ):
        raise ValueError(f"Live benchmark case {case_id} {key} must be a string array")
    patterns = tuple(item)
    for pattern in patterns:
        if len(pattern) > 500:
            raise ValueError(
                f"Live benchmark case {case_id} {key} pattern exceeds 500 characters"
            )
        try:
            re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise ValueError(
                f"Live benchmark case {case_id} {key} has invalid regex {pattern!r}: {exc}"
            ) from exc
    return patterns


def parse_regex_groups(
    value: Mapping[str, object],
    *,
    case_id: str,
) -> tuple[LiveBenchmarkRegexGroup, ...]:
    item = value.get("answer_regex_groups", [])
    if not isinstance(item, list):
        raise ValueError(
            f"Live benchmark case {case_id} answer_regex_groups must be an array"
        )
    groups: list[LiveBenchmarkRegexGroup] = []
    for index, group_value in enumerate(item):
        label = f"Live benchmark case {case_id} answer_regex_groups[{index}]"
        if not isinstance(group_value, dict) or not all(
            isinstance(key, str) for key in group_value
        ):
            raise ValueError(f"{label} must be an object")
        unknown = sorted(set(group_value) - _REGEX_GROUP_KEYS)
        if unknown:
            raise ValueError(f"{label} has unknown fields: {unknown}")
        patterns = parse_regex_list(group_value, "patterns", case_id=label)
        if not patterns:
            raise ValueError(f"{label} patterns must not be empty")
        if len(patterns) != len(set(patterns)):
            raise ValueError(f"{label} patterns must be unique")
        minimum = group_value.get("minimum")
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not 1 <= minimum <= len(patterns)
        ):
            raise ValueError(
                f"{label} minimum must be between 1 and the number of patterns"
            )
        case_sensitive = group_value.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise ValueError(f"{label} case_sensitive must be a boolean")
        groups.append(
            LiveBenchmarkRegexGroup(
                patterns=patterns,
                minimum=minimum,
                case_sensitive=case_sensitive,
            )
        )
    return tuple(groups)
