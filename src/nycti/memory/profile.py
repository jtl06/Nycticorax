from __future__ import annotations

import re


MENTION_MARKER_RE = re.compile(
    r"(?:\buser_id=\d+\b|<@!?\d+>|@[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


def clean_profile_markdown(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("markdown"):
            cleaned = cleaned[8:].strip()
    lines = []
    for line in cleaned.splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            lines.append(normalized)
    cleaned = "\n".join(lines)
    return cleaned[:600]


def strip_noncaller_profile_lines(profile_md: str) -> str:
    lines: list[str] = []
    for line in profile_md.splitlines():
        normalized = " ".join(line.strip().split())
        if not normalized:
            continue
        if MENTION_MARKER_RE.search(normalized):
            continue
        lines.append(normalized)
    return "\n".join(lines)
