from __future__ import annotations


EFFICIENCY_FEATURES = frozenset(
    {
        "ambient_addressedness",
        "extended_context_summary",
        "deep_research_plan",
        "deep_research_reduce",
        "memory_extract",
        "personal_profile_update",
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


def efficiency_model_extra_body(
    *,
    feature: str,
    candidate_model: str,
    configured_model: str,
) -> dict[str, object] | None:
    if feature not in EFFICIENCY_FEATURES or candidate_model != configured_model:
        return None
    normalized_model = candidate_model.casefold().replace("_", "-")
    if "kimi-k2-5" not in normalized_model and "kimi-k2.5" not in normalized_model:
        return None
    return {"chat_template_kwargs": {"thinking": False}}
