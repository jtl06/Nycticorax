from __future__ import annotations

import json
import re
from typing import Mapping


_STRUCTURED_OUTPUT_REQUEST_RE = re.compile(
    r"\b(?:json|jsonl|machine[- ]readable|structured\s+(?:data|output|response))\b",
    re.IGNORECASE,
)
_ANSWER_FIELDS = (
    "answer",
    "response",
    "reply",
    "text",
    "summary",
    "read",
    "analysis",
)
_QUALIFIER_FIELDS = ("caveat", "note", "warning")


def normalize_discord_answer(answer: str, *, request_text: str = "") -> str:
    """Unwrap accidental JSON answer envelopes while preserving requested JSON."""

    if _STRUCTURED_OUTPUT_REQUEST_RE.search(request_text):
        return answer
    payload = _parse_complete_json_object(answer)
    if payload is None:
        return answer

    primary_key = next(
        (
            key
            for key in _ANSWER_FIELDS
            if isinstance(payload.get(key), str) and str(payload[key]).strip()
        ),
        None,
    )
    if primary_key is None:
        return answer

    primary = str(payload[primary_key]).strip()
    details = _render_supporting_fields(payload, excluded={primary_key, *_QUALIFIER_FIELDS})
    qualifiers = [
        str(payload[key]).strip()
        for key in _QUALIFIER_FIELDS
        if isinstance(payload.get(key), str) and str(payload[key]).strip()
    ]
    sections = [primary]
    if details:
        sections.append(details)
    sections.extend(qualifiers)
    return "\n\n".join(dict.fromkeys(sections))


def _parse_complete_json_object(text: str) -> Mapping[str, object] | None:
    candidate = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
    if fence is not None:
        candidate = fence.group(1).strip()
    if not candidate.startswith("{") or not candidate.endswith("}"):
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _render_supporting_fields(
    payload: Mapping[str, object],
    *,
    excluded: set[str],
) -> str:
    lines: list[str] = []
    as_of = payload.get("as_of")
    if isinstance(as_of, str) and as_of.strip():
        lines.append(f"As of {as_of.strip()}.")
        excluded.add("as_of")

    for key, value in payload.items():
        if key in excluded or value is None:
            continue
        rendered = _render_value(value)
        if not rendered:
            continue
        label = key.replace("_", " ").strip().capitalize()
        lines.append(f"{label}: {rendered}")
    return "\n".join(lines)


def _render_value(value: object) -> str:
    if isinstance(value, Mapping):
        parts = [
            f"{str(key).replace('_', ' ')} {rendered}"
            for key, item in value.items()
            if (rendered := _render_value(item))
        ]
        return "; ".join(parts)
    if isinstance(value, list):
        return ", ".join(rendered for item in value if (rendered := _render_value(item)))
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""
