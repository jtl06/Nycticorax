from __future__ import annotations

import re

from nycti.youtube.models import YouTubeTranscriptResponse, YouTubeTranscriptSegment

DEFAULT_TRANSCRIPT_MAX_CHARS = 6000


def format_youtube_transcript_message(
    response: YouTubeTranscriptResponse,
    *,
    query: str | None = None,
    max_chars: int = DEFAULT_TRANSCRIPT_MAX_CHARS,
) -> str:
    language = response.language_name
    if response.language_code and response.language_code not in language:
        language = f"{language} ({response.language_code})"
    track_type = "auto-generated" if response.is_generated else "manual"
    lines = [
        f"YouTube transcript for: https://www.youtube.com/watch?v={response.video_id}",
        f"Language: {language}; track: {track_type}",
        "Use this as evidence; do not paste the transcript block verbatim unless explicitly asked.",
    ]
    if query:
        lines.append(f"Focus: {query}")
    lines.append("")
    lines.extend(_format_segments(response.segments, query=query, max_chars=max_chars))
    return "\n".join(lines).strip()


def format_youtube_transcript_for_summary(
    response: YouTubeTranscriptResponse,
    *,
    query: str | None = None,
    max_chars: int = DEFAULT_TRANSCRIPT_MAX_CHARS,
) -> str:
    lines = _format_segments(response.segments, query=query, max_chars=max_chars)
    return "\n".join(lines).strip()


def format_youtube_transcript_summary_message(
    response: YouTubeTranscriptResponse,
    *,
    summary: str,
    query: str | None = None,
) -> str:
    language = response.language_name
    if response.language_code and response.language_code not in language:
        language = f"{language} ({response.language_code})"
    track_type = "auto-generated" if response.is_generated else "manual"
    lines = [
        f"YouTube transcript summary for: https://www.youtube.com/watch?v={response.video_id}",
        f"Language: {language}; track: {track_type}",
        "This is a capped efficiency-model summary of the transcript. Use it as evidence for the final reply.",
    ]
    if query:
        lines.append(f"Focus: {query}")
    lines.append("")
    lines.append(summary.strip() or "(summary was empty)")
    return "\n".join(lines).strip()


def _format_segments(
    segments: list[YouTubeTranscriptSegment],
    *,
    query: str | None,
    max_chars: int,
) -> list[str]:
    selected = _select_focused_segments(segments, query=query)
    if not selected:
        return ["No transcript text was returned."]

    chunks: list[str] = []
    current_start = selected[0].start_seconds
    current_text: list[str] = []
    for segment in selected:
        if not current_text:
            current_start = segment.start_seconds
        current_text.append(segment.text)
        joined = " ".join(current_text)
        if len(joined) >= 520:
            chunks.append(f"[{_format_timestamp(current_start)}] {joined}")
            current_text = []
    if current_text:
        chunks.append(f"[{_format_timestamp(current_start)}] {' '.join(current_text)}")

    output: list[str] = []
    remaining = max(max_chars, 500)
    for chunk in chunks:
        if len(chunk) + 1 > remaining:
            if remaining > 80:
                output.append(chunk[: remaining - 15].rstrip() + " [truncated]")
            else:
                output.append("[transcript truncated]")
            break
        output.append(chunk)
        remaining -= len(chunk) + 1
    return output


def _select_focused_segments(
    segments: list[YouTubeTranscriptSegment],
    *,
    query: str | None,
) -> list[YouTubeTranscriptSegment]:
    terms = _query_terms(query or "")
    if not terms:
        return segments
    scored: list[tuple[int, int]] = []
    for index, segment in enumerate(segments):
        normalized = segment.text.casefold()
        score = sum(normalized.count(term) for term in terms)
        if score:
            scored.append((score, index))
    if not scored:
        return segments[:80]
    focused_indexes: set[int] = set()
    for _, index in sorted(scored, reverse=True)[:8]:
        focused_indexes.update(range(max(index - 1, 0), min(index + 2, len(segments))))
    return [segment for index, segment in enumerate(segments) if index in focused_indexes]


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", query.casefold()):
        if term not in terms:
            terms.append(term)
    return terms


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
