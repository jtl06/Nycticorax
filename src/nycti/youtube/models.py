from __future__ import annotations

from dataclasses import dataclass


class YouTubeTranscriptError(Exception):
    pass


class YouTubeTranscriptDisabledError(YouTubeTranscriptError):
    pass


class YouTubeTranscriptUnavailableError(YouTubeTranscriptError):
    pass


class YouTubeTranscriptDataError(YouTubeTranscriptError):
    pass


class YouTubeTranscriptHTTPError(YouTubeTranscriptError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeTranscriptSegment:
    start_seconds: float
    duration_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class YouTubeTranscriptResponse:
    video_id: str
    requested_url: str
    transcript_url: str
    language_code: str
    language_name: str
    is_generated: bool
    segments: list[YouTubeTranscriptSegment]
