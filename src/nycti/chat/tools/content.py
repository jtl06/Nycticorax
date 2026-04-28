from __future__ import annotations

import logging

from nycti.browser import (
    BrowserToolDataError,
    BrowserToolDisabledError,
    BrowserToolRuntimeError,
    BrowserToolUnavailableError,
    format_browser_extract_message,
)
from nycti.message_context import (
    DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    fetch_older_context_lines,
)
from nycti.python_sandbox import PythonSandboxError, run_python_sandbox
from nycti.tavily.formatting import (
    format_tavily_extract_message,
    format_tavily_image_search_message,
    format_tavily_search_message,
)
from nycti.tavily.models import TavilyAPIKeyMissingError, TavilyDataError, TavilyHTTPError
from nycti.youtube import (
    YouTubeTranscriptDataError,
    YouTubeTranscriptDisabledError,
    YouTubeTranscriptHTTPError,
    YouTubeTranscriptUnavailableError,
    format_youtube_transcript_for_summary,
    format_youtube_transcript_summary_message,
)

LOGGER = logging.getLogger(__name__)


class ContentToolMixin:
    def _execute_python_tool(self, *, code: str) -> str:
        if not getattr(self.settings, "python_tool_enabled", False):
            return "Python execution failed because PYTHON_TOOL_ENABLED is false."
        try:
            result = run_python_sandbox(
                code,
                timeout_seconds=getattr(self.settings, "python_tool_timeout_seconds", 3.0),
                max_output_chars=getattr(self.settings, "python_tool_max_output_chars", 4000),
            )
        except (PythonSandboxError, SyntaxError, ValueError) as exc:
            return f"Python execution failed: {exc}"
        truncation = "\n(output truncated)" if result.truncated else ""
        return f"Python result ({result.elapsed_ms} ms):\n```text\n{result.output}{truncation}\n```"

    async def _execute_web_search_tool(
        self,
        *,
        query: str,
    ) -> str:
        try:
            search_response = await self.tavily_client.search(query=query, max_results=5)
        except TavilyAPIKeyMissingError:
            return "Web search failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            return f"Web search for `{query}` failed because the Tavily request failed."
        except TavilyDataError:
            return f"Web search for `{query}` failed because the Tavily response was malformed."
        return format_tavily_search_message(search_response, max_items=3)

    async def _execute_get_channel_context_tool(
        self,
        *,
        mode: str,
        multiplier: int,
        expand: bool,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, int]:
        if channel_id is None or source_message_id is None:
            return "Channel context fetch failed because this request's source channel/message could not be resolved.", 0
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return "Channel context fetch failed because the channel could not be fetched.", 0
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None or not hasattr(channel, "history"):
            return "Channel context fetch failed because this channel does not expose message history.", 0
        try:
            source_message = await fetch_message(source_message_id)
        except Exception:
            return "Channel context fetch failed because the source message could not be fetched.", 0
        base_multiplier = 5 if mode == "raw" else 25
        message_limit = self.settings.channel_context_limit * base_multiplier * multiplier
        line_cap = EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT if expand else DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT
        lines = await fetch_older_context_lines(
            channel,
            before=source_message,
            recent_limit=self.settings.channel_context_limit,
            limit=message_limit,
            content_char_limit=line_cap,
        )
        if not lines:
            return "Channel context fetch found no older messages beyond the default recent window.", 0
        if mode == "raw":
            return (
                "Older Discord channel context (raw, oldest to newest). "
                f"Per-line text cap: {line_cap} chars. Do not paste this block verbatim; "
                "synthesize only what is relevant unless the user explicitly requested raw logs:\n"
                + "\n".join(lines)
            ), 0
        result = await self.llm_client.complete_chat(
            model=self.settings.openai_memory_model,
            feature="extended_context_summary",
            max_tokens=500,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize older Discord channel context for another assistant. "
                        "Keep durable facts, decisions, unresolved questions, and useful references. "
                        "Ignore low-value chatter. Do not invent details. Do not produce a transcript."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Older channel messages, oldest to newest:\n"
                        + "\n".join(lines)
                        + "\n\nReturn a concise bullet summary under 180 words."
                    ),
                },
            ],
        )
        await self._record_auxiliary_llm_usage(
            usage=result.usage,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        return (
            "Older Discord channel context (summary):\n"
            + (result.text.strip() or "(summary was empty)")
        ), result.usage.total_tokens

    async def _execute_image_search_tool(
        self,
        *,
        query: str,
    ) -> str:
        try:
            search_response = await self.tavily_client.image_search(query=query, max_results=5)
        except TavilyAPIKeyMissingError:
            return "Image search failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            return f"Image search for `{query}` failed because the Tavily request failed."
        except TavilyDataError:
            return f"Image search for `{query}` failed because the Tavily response was malformed."
        return format_tavily_image_search_message(search_response, max_items=3)

    async def _execute_extract_url_tool(
        self,
        *,
        url: str,
        query: str | None,
    ) -> str:
        try:
            extract_response = await self.tavily_client.extract(url=url, query=query)
        except TavilyAPIKeyMissingError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return "URL extraction failed because TAVILY_API_KEY is not configured."
        except TavilyHTTPError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return f"URL extraction for `{url}` failed because the Tavily request failed."
        except TavilyDataError:
            browser_fallback = await self._try_browser_extract_fallback(url=url, query=query)
            if browser_fallback is not None:
                return browser_fallback
            return f"URL extraction for `{url}` failed because the Tavily response was malformed."
        return format_tavily_extract_message(extract_response)

    async def _execute_browser_extract_tool(
        self,
        *,
        url: str,
        query: str | None,
        headed: bool,
    ) -> str:
        if self.browser_client is None:
            return "Browser extract failed because browser tooling is not configured."
        try:
            result = await self.browser_client.extract(url=url, query=query, headed=headed)
        except BrowserToolDisabledError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolUnavailableError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolDataError as exc:
            return f"Browser extract failed: {exc}"
        except BrowserToolRuntimeError as exc:
            return f"Browser extract failed: {exc}"
        return format_browser_extract_message(result)

    async def _execute_youtube_transcript_tool(
        self,
        *,
        url: str,
        query: str | None,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> tuple[str, int]:
        if self.youtube_client is None:
            return "YouTube transcript extraction failed because YouTube transcript tooling is not configured.", 0
        try:
            result = await self.youtube_client.get_transcript(url=url)
        except YouTubeTranscriptDisabledError:
            return "YouTube transcript extraction failed because YOUTUBE_TRANSCRIPT_ENABLED is false.", 0
        except YouTubeTranscriptUnavailableError as exc:
            return f"YouTube transcript extraction failed: {exc}", 0
        except YouTubeTranscriptHTTPError as exc:
            return f"YouTube transcript extraction failed: {exc}", 0
        except YouTubeTranscriptDataError as exc:
            return f"YouTube transcript extraction failed: {exc}", 0

        transcript = format_youtube_transcript_for_summary(
            result,
            query=query,
            max_chars=getattr(self.settings, "youtube_transcript_max_chars", 6000),
        )
        try:
            summary_result = await self.llm_client.complete_chat(
                model=self.settings.openai_memory_model,
                feature="youtube_transcript_summary",
                max_tokens=500,
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize a YouTube transcript for another assistant. "
                            "Keep the speaker's main claims, concrete facts, decisions, examples, and caveats. "
                            "Preserve useful timestamps from the transcript lines. Do not invent details. "
                            "Do not output a transcript."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Video URL: https://www.youtube.com/watch?v={result.video_id}\n"
                            + (f"Focus query: {query}\n" if query else "")
                            + "Timestamped transcript evidence, capped:\n"
                            + transcript
                            + "\n\nReturn a concise summary under 220 words. "
                            "If the focus query is present, emphasize only the relevant parts."
                        ),
                    },
                ],
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("YouTube transcript summary failed.")
            return "YouTube transcript extraction succeeded, but transcript summarization failed.", 0

        await self._record_auxiliary_llm_usage(
            usage=summary_result.usage,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        return (
            format_youtube_transcript_summary_message(
                result,
                summary=summary_result.text,
                query=query,
            ),
            summary_result.usage.total_tokens,
        )

    async def _try_browser_extract_fallback(
        self,
        *,
        url: str,
        query: str | None,
    ) -> str | None:
        if self.browser_client is None:
            return None
        try:
            result = await self.browser_client.extract(url=url, query=query, headed=False)
        except (BrowserToolDisabledError, BrowserToolUnavailableError, BrowserToolDataError, BrowserToolRuntimeError):
            return None
        return format_browser_extract_message(result)
