import unittest

from nycti.youtube import (
    YouTubeTranscriptClient,
    YouTubeTranscriptDisabledError,
    extract_youtube_video_id,
    format_youtube_transcript_for_summary,
    format_youtube_transcript_message,
    format_youtube_transcript_summary_message,
)


class YouTubeTranscriptClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_transcript_fetches_preferred_track(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, timeout_seconds: float) -> str:
            calls.append(url)
            if "type=list" in url:
                return (
                    '<transcript_list>'
                    '<track id="0" name="" lang_code="es" lang_original="Spanish" lang_translated="Spanish"/>'
                    '<track id="1" name="" lang_code="en" lang_original="English" lang_translated="English"/>'
                    "</transcript_list>"
                )
            return (
                '{"events":['
                '{"tStartMs":0,"dDurationMs":2000,"segs":[{"utf8":"hello "},{"utf8":"world"}]},'
                '{"tStartMs":2500,"dDurationMs":1500,"segs":[{"utf8":"second line"}]}'
                "]}"
            )

        client = YouTubeTranscriptClient(fetch_text=fake_fetch)

        response = await client.get_transcript(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        self.assertEqual(response.video_id, "dQw4w9WgXcQ")
        self.assertEqual(response.language_code, "en")
        self.assertEqual([segment.text for segment in response.segments], ["hello world", "second line"])
        self.assertEqual(len(calls), 2)
        self.assertIn("lang=en", calls[1])

    async def test_get_transcript_falls_back_to_watch_page_caption_tracks(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, timeout_seconds: float) -> str:
            calls.append(url)
            if "type=list" in url:
                return ""
            if "watch?" in url:
                return (
                    '<html><script>ytInitialPlayerResponse = {"captions":'
                    '{"playerCaptionsTracklistRenderer":{"captionTracks":['
                    '{"baseUrl":"https://example.com/caption?lang=en","languageCode":"en",'
                    '"name":{"simpleText":"English"}}'
                    "]}}};</script></html>"
                )
            return (
                '{"events":['
                '{"tStartMs":0,"dDurationMs":2000,"segs":[{"utf8":"fallback caption"}]}'
                "]}"
            )

        client = YouTubeTranscriptClient(fetch_text=fake_fetch)

        response = await client.get_transcript(url="https://youtu.be/dQw4w9WgXcQ")

        self.assertEqual(response.language_name, "English")
        self.assertEqual(response.segments[0].text, "fallback caption")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2], "https://example.com/caption?lang=en&fmt=json3")

    async def test_disabled_client_raises(self) -> None:
        client = YouTubeTranscriptClient(enabled=False, fetch_text=lambda _url, _timeout: "")

        with self.assertRaises(YouTubeTranscriptDisabledError):
            await client.get_transcript(url="https://youtu.be/dQw4w9WgXcQ")


class YouTubeUrlParsingTests(unittest.TestCase):
    def test_extract_youtube_video_id_accepts_common_url_shapes(self) -> None:
        self.assertEqual(extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            extract_youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_youtube_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertIsNone(extract_youtube_video_id("https://example.com/watch?v=dQw4w9WgXcQ"))


class YouTubeTranscriptFormattingTests(unittest.TestCase):
    def test_format_youtube_transcript_focuses_query_matches(self) -> None:
        from nycti.youtube.models import YouTubeTranscriptResponse, YouTubeTranscriptSegment

        response = YouTubeTranscriptResponse(
            video_id="dQw4w9WgXcQ",
            requested_url="https://youtu.be/dQw4w9WgXcQ",
            transcript_url="https://video.google.com/timedtext?v=dQw4w9WgXcQ",
            language_code="en",
            language_name="English",
            is_generated=True,
            segments=[
                YouTubeTranscriptSegment(start_seconds=0, duration_seconds=2, text="intro"),
                YouTubeTranscriptSegment(start_seconds=2, duration_seconds=2, text="deep learning section"),
                YouTubeTranscriptSegment(start_seconds=4, duration_seconds=2, text="details"),
            ],
        )

        formatted = format_youtube_transcript_message(response, query="deep learning", max_chars=2000)

        self.assertIn("track: auto-generated", formatted)
        self.assertIn("Focus: deep learning", formatted)
        self.assertIn("[0:00] intro deep learning section details", formatted)

    def test_format_youtube_transcript_for_summary_caps_transcript(self) -> None:
        from nycti.youtube.models import YouTubeTranscriptResponse, YouTubeTranscriptSegment

        response = YouTubeTranscriptResponse(
            video_id="dQw4w9WgXcQ",
            requested_url="https://youtu.be/dQw4w9WgXcQ",
            transcript_url="https://video.google.com/timedtext?v=dQw4w9WgXcQ",
            language_code="en",
            language_name="English",
            is_generated=False,
            segments=[
                YouTubeTranscriptSegment(start_seconds=0, duration_seconds=2, text="a" * 800),
                YouTubeTranscriptSegment(start_seconds=2, duration_seconds=2, text="b" * 800),
            ],
        )

        formatted = format_youtube_transcript_for_summary(response, max_chars=1000)

        self.assertLessEqual(len(formatted), 1015)
        self.assertIn("[truncated]", formatted)

    def test_format_youtube_transcript_summary_message_does_not_include_raw_segments(self) -> None:
        from nycti.youtube.models import YouTubeTranscriptResponse, YouTubeTranscriptSegment

        response = YouTubeTranscriptResponse(
            video_id="dQw4w9WgXcQ",
            requested_url="https://youtu.be/dQw4w9WgXcQ",
            transcript_url="https://video.google.com/timedtext?v=dQw4w9WgXcQ",
            language_code="en",
            language_name="English",
            is_generated=False,
            segments=[YouTubeTranscriptSegment(start_seconds=0, duration_seconds=2, text="raw line")],
        )

        formatted = format_youtube_transcript_summary_message(
            response,
            summary="Compact summary only.",
            query="topic",
        )

        self.assertIn("YouTube transcript summary for:", formatted)
        self.assertIn("Focus: topic", formatted)
        self.assertIn("Compact summary only.", formatted)
        self.assertNotIn("raw line", formatted)


if __name__ == "__main__":
    unittest.main()
