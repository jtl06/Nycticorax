from __future__ import annotations

import re


SELF_REFERENCE_RE = re.compile(
    r"\b(i|i'm|im|i’ve|i'd|i’ll|i'll|i've|me|my|mine|myself)\b",
    re.IGNORECASE,
)
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


def should_attempt_profile_update(current_message: str) -> bool:
    normalized = " ".join(current_message.strip().split())
    if not normalized:
        return False
    has_self_reference = bool(SELF_REFERENCE_RE.search(normalized))
    has_explicit_mention = bool(MENTION_MARKER_RE.search(normalized))
    if has_explicit_mention and not has_self_reference:
        return False
    return True


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
