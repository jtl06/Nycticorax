from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import html
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from nycti.youtube.models import (
    YouTubeTranscriptDataError,
    YouTubeTranscriptDisabledError,
    YouTubeTranscriptHTTPError,
    YouTubeTranscriptResponse,
    YouTubeTranscriptSegment,
    YouTubeTranscriptUnavailableError,
)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
DEFAULT_PREFERRED_LANGUAGES = ("en", "en-US", "en-GB")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class _TranscriptTrack:
    language_code: str
    language_name: str
    is_generated: bool
    name: str | None
    base_url: str | None = None


FetchText = Callable[[str, float], str]


class YouTubeTranscriptClient:
    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_seconds: float = 10.0,
        preferred_languages: tuple[str, ...] = DEFAULT_PREFERRED_LANGUAGES,
        fetch_text: FetchText | None = None,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.preferred_languages = preferred_languages
        self._fetch_text = fetch_text or _fetch_text

    async def get_transcript(self, *, url: str) -> YouTubeTranscriptResponse:
        if not self.enabled:
            raise YouTubeTranscriptDisabledError("YouTube transcript extraction is disabled.")
        return await asyncio.to_thread(self._get_transcript_sync, url)

    def _get_transcript_sync(self, url: str) -> YouTubeTranscriptResponse:
        video_id = extract_youtube_video_id(url)
        if video_id is None:
            raise YouTubeTranscriptDataError("The URL is not a supported YouTube video URL.")

        tracks = self._fetch_tracks(video_id)
        if not tracks:
            raise YouTubeTranscriptUnavailableError("No transcript tracks were advertised for this video.")

        track = _select_track(tracks, self.preferred_languages)
        transcript_url = _build_transcript_url(video_id, track)
        raw_transcript = self._fetch_text(transcript_url, self.timeout_seconds)
        segments = _parse_transcript(raw_transcript)
        if not segments:
            raise YouTubeTranscriptUnavailableError("The selected transcript track was empty.")

        return YouTubeTranscriptResponse(
            video_id=video_id,
            requested_url=url,
            transcript_url=transcript_url,
            language_code=track.language_code,
            language_name=track.language_name,
            is_generated=track.is_generated,
            segments=segments,
        )

    def _fetch_tracks(self, video_id: str) -> list[_TranscriptTrack]:
        track_url = "https://video.google.com/timedtext?" + urlencode(
            {"type": "list", "v": video_id}
        )
        raw_tracks = self._fetch_text(track_url, self.timeout_seconds)
        if not raw_tracks.strip():
            return self._fetch_tracks_from_watch_page(video_id)
        try:
            root = ET.fromstring(raw_tracks)
        except ET.ParseError as exc:
            raise YouTubeTranscriptDataError("YouTube returned malformed transcript metadata.") from exc
        tracks: list[_TranscriptTrack] = []
        for node in root.findall("track"):
            language_code = str(node.attrib.get("lang_code", "")).strip()
            if not language_code:
                continue
            tracks.append(
                _TranscriptTrack(
                    language_code=language_code,
                    language_name=(
                        str(node.attrib.get("lang_translated", "")).strip()
                        or str(node.attrib.get("lang_original", "")).strip()
                        or language_code
                    ),
                    is_generated=str(node.attrib.get("kind", "")).strip().lower() == "asr",
                    name=str(node.attrib.get("name", "")).strip() or None,
                )
            )
        if tracks:
            return tracks
        return self._fetch_tracks_from_watch_page(video_id)

    def _fetch_tracks_from_watch_page(self, video_id: str) -> list[_TranscriptTrack]:
        watch_url = "https://www.youtube.com/watch?" + urlencode({"v": video_id, "hl": "en"})
        raw_page = self._fetch_text(watch_url, self.timeout_seconds)
        player_response = _extract_player_response(raw_page)
        if player_response is None:
            return []
        captions = player_response.get("captions")
        if not isinstance(captions, dict):
            return []
        track_list = captions.get("playerCaptionsTracklistRenderer")
        if not isinstance(track_list, dict):
            return []
        caption_tracks = track_list.get("captionTracks")
        if not isinstance(caption_tracks, list):
            return []
        tracks: list[_TranscriptTrack] = []
        for item in caption_tracks:
            if not isinstance(item, dict):
                continue
            language_code = str(item.get("languageCode", "")).strip()
            base_url = str(item.get("baseUrl", "")).strip()
            if not language_code or not base_url:
                continue
            tracks.append(
                _TranscriptTrack(
                    language_code=language_code,
                    language_name=_caption_track_name(item) or language_code,
                    is_generated=str(item.get("kind", "")).strip().lower() == "asr",
                    name=None,
                    base_url=base_url,
                )
            )
        return tracks


def extract_youtube_video_id(url_or_id: str) -> str | None:
    cleaned = url_or_id.strip()
    if _is_video_id(cleaned):
        return cleaned
    parsed = urlparse(cleaned)
    host = parsed.netloc.casefold()
    if host not in YOUTUBE_HOSTS:
        return None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if _is_video_id(candidate) else None
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if _is_video_id(query_id):
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"} and _is_video_id(parts[1]):
        return parts[1]
    return None


def is_youtube_url(text: str) -> bool:
    return extract_youtube_video_id(text) is not None


def _is_video_id(value: str) -> bool:
    if len(value) != 11:
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in value)


def _fetch_text(url: str, timeout_seconds: float) -> str:
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise YouTubeTranscriptHTTPError(f"YouTube transcript request failed with HTTP {exc.code}.") from exc
    except URLError as exc:
        raise YouTubeTranscriptHTTPError("YouTube transcript request failed.") from exc
    except TimeoutError as exc:
        raise YouTubeTranscriptHTTPError("YouTube transcript request timed out.") from exc


def _select_track(
    tracks: list[_TranscriptTrack],
    preferred_languages: tuple[str, ...],
) -> _TranscriptTrack:
    normalized_preferences = tuple(language.casefold() for language in preferred_languages)
    for generated in (False, True):
        for preferred in normalized_preferences:
            for track in tracks:
                code = track.language_code.casefold()
                if track.is_generated == generated and (code == preferred or code.startswith(preferred + "-")):
                    return track
    for generated in (False, True):
        for track in tracks:
            if track.is_generated == generated and track.language_code.casefold().startswith("en"):
                return track
    for generated in (False, True):
        for track in tracks:
            if track.is_generated == generated:
                return track
    return tracks[0]


def _build_transcript_url(video_id: str, track: _TranscriptTrack) -> str:
    if track.base_url:
        separator = "&" if "?" in track.base_url else "?"
        if "fmt" in parse_qs(urlparse(track.base_url).query):
            return track.base_url
        return track.base_url + separator + urlencode({"fmt": "json3"})
    params = {
        "v": video_id,
        "lang": track.language_code,
        "fmt": "json3",
    }
    if track.is_generated:
        params["kind"] = "asr"
    if track.name:
        params["name"] = track.name
    return "https://video.google.com/timedtext?" + urlencode(params)


def _extract_player_response(raw_page: str) -> dict[str, object] | None:
    match = re.search(r"ytInitialPlayerResponse\s*=", raw_page)
    if match is None:
        return None
    json_start = raw_page.find("{", match.end())
    if json_start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw_page[json_start:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _caption_track_name(item: dict[str, object]) -> str | None:
    name = item.get("name")
    if not isinstance(name, dict):
        return None
    simple_text = name.get("simpleText")
    if isinstance(simple_text, str) and simple_text.strip():
        return simple_text.strip()
    runs = name.get("runs")
    if not isinstance(runs, list):
        return None
    parts: list[str] = []
    for run in runs:
        if isinstance(run, dict):
            text = str(run.get("text", "")).strip()
            if text:
                parts.append(text)
    return " ".join(parts).strip() or None


def _parse_transcript(raw_transcript: str) -> list[YouTubeTranscriptSegment]:
    stripped = raw_transcript.strip()
    if not stripped:
        return []
    if stripped.startswith("{"):
        return _parse_json3_transcript(stripped)
    return _parse_xml_transcript(stripped)


def _parse_json3_transcript(raw_transcript: str) -> list[YouTubeTranscriptSegment]:
    try:
        payload = json.loads(raw_transcript)
    except json.JSONDecodeError as exc:
        raise YouTubeTranscriptDataError("YouTube returned malformed transcript JSON.") from exc
    events = payload.get("events")
    if not isinstance(events, list):
        raise YouTubeTranscriptDataError("YouTube transcript JSON did not include events.")
    segments: list[YouTubeTranscriptSegment] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        text_parts: list[str] = []
        segs = event.get("segs")
        if isinstance(segs, list):
            for seg in segs:
                if isinstance(seg, dict):
                    text = str(seg.get("utf8", ""))
                    if text:
                        text_parts.append(text)
        text = " ".join("".join(text_parts).split()).strip()
        if not text:
            continue
        start_ms = _float_value(event.get("tStartMs"))
        duration_ms = _float_value(event.get("dDurationMs"))
        segments.append(
            YouTubeTranscriptSegment(
                start_seconds=start_ms / 1000,
                duration_seconds=duration_ms / 1000,
                text=html.unescape(text),
            )
        )
    return segments


def _parse_xml_transcript(raw_transcript: str) -> list[YouTubeTranscriptSegment]:
    try:
        root = ET.fromstring(raw_transcript)
    except ET.ParseError as exc:
        raise YouTubeTranscriptDataError("YouTube returned malformed transcript XML.") from exc
    segments: list[YouTubeTranscriptSegment] = []
    for node in root.findall("text"):
        text = " ".join("".join(node.itertext()).split()).strip()
        if not text:
            continue
        segments.append(
            YouTubeTranscriptSegment(
                start_seconds=_float_value(node.attrib.get("start")),
                duration_seconds=_float_value(node.attrib.get("dur")),
                text=html.unescape(text),
            )
        )
    return segments


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
