from __future__ import annotations


EFFICIENCY_FEATURES = frozenset(
    {
        "ambient_addressedness",
        "extended_context_summary",
        "deep_research_plan",
        "deep_research_reduce",
        "memory_consolidate",
        "memory_extract",
        "procedure_extract",
        "youtube_transcript_summary",
    }
)
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def reasoning_effort_for_feature(
    *,
    feature: str,
    foreground_effort: str,
    efficiency_effort: str,
    override: str | None = None,
) -> str:
    if override:
        return override
    if feature in EFFICIENCY_FEATURES and efficiency_effort:
        return efficiency_effort
    return foreground_effort


def reasoning_effort_for_model(*, model: str, effort: str) -> str:
    if not effort:
        return ""
    normalized = model.rsplit("/", 1)[-1].strip().casefold()
    if normalized.startswith(REASONING_MODEL_PREFIXES):
        return effort
    return ""
