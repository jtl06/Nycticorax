from __future__ import annotations


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
