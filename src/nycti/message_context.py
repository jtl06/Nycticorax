from __future__ import annotations

import asyncio
import time

from nycti.message_context_formatting import (
    DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT,
    clean_trigger_content,
    collect_message_members,
    dedupe_image_refs,
    dedupe_lines,
    discord,
    expand_user_mentions,
    fetch_older_context_lines,
    format_message_line,
    image_refs_for_message,
    is_within_recent_context_window,
    message_has_visible_content,
)
from nycti.message_context_source import DiscordMessageContextSource
from nycti.timing import elapsed_ms


class MessageContextCollector(DiscordMessageContextSource):
    """Assemble bounded Discord messages and images into prompt-ready context."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        channel_context_limit: int,
        max_reply_chain_depth: int,
        max_linked_message_count: int,
        max_context_image_count: int,
        anchor_context_per_side: int,
    ) -> None:
        super().__init__(
            bot=bot,
            channel_context_limit=channel_context_limit,
            max_reply_chain_depth=max_reply_chain_depth,
            max_linked_message_count=max_linked_message_count,
            anchor_context_per_side=anchor_context_per_side,
        )
        self.max_context_image_count = max_context_image_count

    async def build_message_context(
        self,
        message: discord.Message,
    ) -> tuple[list[str], list[str], list[str]]:
        context_lines, image_urls, image_context_lines, _members = (
            await self.build_message_context_with_members(message)
        )
        return context_lines, image_urls, image_context_lines

    async def build_message_context_with_members(
        self,
        message: discord.Message,
        *,
        timing_metrics: dict[str, int] | None = None,
    ) -> tuple[list[str], list[str], list[str], list[object]]:
        context_started_at = time.perf_counter()

        async def fetch_history() -> tuple[list[discord.Message], int]:
            started_at = time.perf_counter()
            messages = await self._fetch_context_messages(message.channel, before=message)
            return messages, elapsed_ms(started_at)

        async def fetch_reply_chain() -> tuple[list[discord.Message], int]:
            started_at = time.perf_counter()
            messages = await self._collect_reply_chain_messages(message)
            return messages, elapsed_ms(started_at)

        async with asyncio.TaskGroup() as task_group:
            history_task = task_group.create_task(fetch_history())
            reply_task = task_group.create_task(fetch_reply_chain())

            reply_chain_messages, reply_fetch_ms = await reply_task
            stage_started_at = time.perf_counter()
            reply_lines = [
                format_message_line(item, prefix=f"reply depth {depth}", include_timestamp=True)
                for depth, item in enumerate(reply_chain_messages, start=1)
                if message_has_visible_content(item)
            ]
            if timing_metrics is not None:
                timing_metrics["ctx_reply_ms"] = reply_fetch_ms + elapsed_ms(stage_started_at)

            stage_started_at = time.perf_counter()
            linked_messages = await self._collect_linked_messages(
                message,
                reply_chain_messages=reply_chain_messages,
            )
            linked_lines = [
                format_message_line(item, prefix="linked message", include_timestamp=True)
                for item in linked_messages
                if message_has_visible_content(item)
            ]
            _record_timing(timing_metrics, "ctx_links_ms", stage_started_at)

            stage_started_at = time.perf_counter()
            anchor_context_messages = await self._collect_anchor_context_messages(
                message,
                anchor_messages=[*reply_chain_messages, *linked_messages],
            )
            anchor_context_lines = [
                format_message_line(item, prefix="anchor context", include_timestamp=True)
                for item in anchor_context_messages
                if message_has_visible_content(item)
            ]
            _record_timing(timing_metrics, "ctx_anchor_ms", stage_started_at)

        history_messages, history_fetch_ms = await history_task
        stage_started_at = time.perf_counter()
        history_lines = [
            format_message_line(item)
            for item in history_messages
            if message_has_visible_content(item)
            and is_within_recent_context_window(item, reference=message)
        ]
        if message_has_visible_content(message):
            history_lines.append(format_message_line(message))
        if timing_metrics is not None:
            timing_metrics["ctx_recent_ms"] = history_fetch_ms + elapsed_ms(stage_started_at)

        stage_started_at = time.perf_counter()
        context_lines = self._compose_context_lines(
            reply_lines=reply_lines,
            linked_lines=linked_lines,
            anchor_context_lines=anchor_context_lines,
            history_lines=history_lines,
        )
        image_refs = self._collect_image_refs(
            message=message,
            history_messages=history_messages,
            reply_chain_messages=reply_chain_messages,
            linked_messages=linked_messages,
            anchor_context_messages=anchor_context_messages,
        )
        deduped_image_refs = dedupe_image_refs(
            image_refs,
            max_count=self.max_context_image_count,
        )
        image_urls = [url for _, url in deduped_image_refs]
        image_context_lines = [
            f"- image {index}: {label}"
            for index, (label, _) in enumerate(deduped_image_refs, start=1)
        ]
        context_members = collect_message_members(
            [
                message,
                *history_messages,
                *reply_chain_messages,
                *linked_messages,
                *anchor_context_messages,
            ]
        )
        _record_timing(timing_metrics, "ctx_msg_format_ms", stage_started_at)
        _record_timing(timing_metrics, "ctx_discord_ms", context_started_at)
        return context_lines, image_urls, image_context_lines, context_members

    def _compose_context_lines(
        self,
        *,
        reply_lines: list[str],
        linked_lines: list[str],
        anchor_context_lines: list[str],
        history_lines: list[str],
    ) -> list[str]:
        direct_lines = dedupe_lines(reply_lines + linked_lines)
        nearby_anchor_lines = dedupe_lines(anchor_context_lines)
        recent_history = dedupe_lines(history_lines)
        reserve_for_recent = 1 if recent_history else 0
        pinned_budget = max(self.channel_context_limit - reserve_for_recent, 0)
        direct_budget = min(
            len(direct_lines),
            pinned_budget - 1 if nearby_anchor_lines and pinned_budget > 1 else pinned_budget,
        )
        selected_direct = direct_lines[:direct_budget]
        selected_anchor = nearby_anchor_lines[: max(pinned_budget - len(selected_direct), 0)]
        pinned_lines = dedupe_lines(selected_direct + selected_anchor)
        remaining_budget = self.channel_context_limit - len(pinned_lines)
        selected_recent = recent_history[-remaining_budget:] if remaining_budget > 0 else []
        return dedupe_lines(pinned_lines + selected_recent)

    def _collect_image_refs(
        self,
        *,
        message: discord.Message,
        history_messages: list[discord.Message],
        reply_chain_messages: list[discord.Message],
        linked_messages: list[discord.Message],
        anchor_context_messages: list[discord.Message],
    ) -> list[tuple[str, str]]:
        labeled_messages = [
            ("current message", message),
            *[
                (f"reply depth {depth}", item)
                for depth, item in enumerate(reply_chain_messages, start=1)
            ],
            *[("linked message", item) for item in linked_messages],
            *[("recent context", item) for item in history_messages],
            *[("anchor context", item) for item in anchor_context_messages],
        ]
        image_refs: list[tuple[str, str]] = []
        for label, item in labeled_messages:
            image_refs.extend(
                image_refs_for_message(
                    item,
                    label=label,
                    image_limit=self.max_context_image_count,
                )
            )
        return image_refs


def _record_timing(
    timing_metrics: dict[str, int] | None,
    key: str,
    started_at: float,
) -> None:
    if timing_metrics is not None:
        timing_metrics[key] = elapsed_ms(started_at)


__all__ = [
    "DEFAULT_CONTEXT_LINE_TEXT_CHAR_LIMIT",
    "EXPANDED_CONTEXT_LINE_TEXT_CHAR_LIMIT",
    "MessageContextCollector",
    "clean_trigger_content",
    "collect_message_members",
    "dedupe_image_refs",
    "dedupe_lines",
    "expand_user_mentions",
    "fetch_older_context_lines",
    "format_message_line",
    "image_refs_for_message",
    "message_has_visible_content",
]
