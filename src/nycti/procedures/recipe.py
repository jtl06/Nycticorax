from __future__ import annotations

import json
from typing import Mapping


def argument_field_names(arguments: str) -> tuple[str, ...]:
    """Return bounded argument keys without retaining any argument values."""

    try:
        payload = json.loads(arguments)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(sorted(str(key)[:64] for key in payload)[:12])


def execution_recipe_from_steps(raw_steps: object) -> tuple[str, ...]:
    """Build a value-free tool recipe from serialized agent step telemetry."""

    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw_steps, list):
        return ()

    batches: dict[int, list[str]] = {}
    for raw in raw_steps:
        if not isinstance(raw, Mapping) or not raw.get("tool_name"):
            continue
        if str(raw.get("status", "")).casefold() != "ok":
            continue
        details = raw.get("details")
        details = details if isinstance(details, Mapping) else {}
        batch_index = _positive_int(details.get("batch_index"), default=len(batches) + 1)
        raw_fields = details.get("argument_fields")
        fields = (
            tuple(str(field)[:64] for field in raw_fields[:12])
            if isinstance(raw_fields, list)
            else ()
        )
        tool = str(raw["tool_name"])[:64]
        rendered = f"{tool}({', '.join(fields)})" if fields else tool
        if rendered not in batches.setdefault(batch_index, []):
            batches[batch_index].append(rendered)
    return tuple(
        f"batch {batch_index}: {' + '.join(tools)}"
        for batch_index, tools in sorted(batches.items())
        if tools
    )[:8]


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
