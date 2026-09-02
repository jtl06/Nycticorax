from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from nycti.live_benchmarks import LiveBenchmarkCase, LiveBenchmarkDiscordMessage
from nycti.message_context import MessageContextCollector


@dataclass(frozen=True, slots=True)
class LiveBenchmarkMessageContext:
    context_lines: tuple[str, ...] = ()
    image_attachment_urls: tuple[str, ...] = ()
    image_context_lines: tuple[str, ...] = ()
    timing_metrics: dict[str, int] | None = None


async def build_live_benchmark_message_context(
    case: LiveBenchmarkCase,
    *,
    template_collector: MessageContextCollector | None,
    now: datetime,
) -> LiveBenchmarkMessageContext:
    """Run synthetic messages through the same collector used by Discord traffic."""
    if case.discord.is_empty:
        return LiveBenchmarkMessageContext(timing_metrics={})
    if template_collector is None:
        raise ValueError(
            f"Benchmark case {case.case_id} requires a Discord context collector"
        )

    guild = SimpleNamespace(id=9_001)
    authors: dict[str, Any] = {}

    def author(name: str) -> Any:
        key = name.casefold()
        if key not in authors:
            author_id = 90_100 + len(authors)
            authors[key] = SimpleNamespace(
                id=author_id,
                name=name,
                display_name=name,
                global_name=name,
            )
        return authors[key]

    channel = _SyntheticChannel(channel_id=9_002, guild=guild)
    recent = [
        _build_message(spec, message_id=1_000 + index, now=now, author=author(spec.author))
        for index, spec in enumerate(case.discord.recent_messages)
    ]
    reply_chain = [
        _build_message(spec, message_id=2_000 + index, now=now, author=author(spec.author))
        for index, spec in enumerate(case.discord.reply_chain)
    ]
    for index, message in enumerate(reply_chain[:-1]):
        referenced = reply_chain[index + 1]
        message.reference = SimpleNamespace(
            message_id=referenced.id,
            resolved=referenced,
            cached_message=referenced,
        )
    current_author = author("benchmark")
    current = SimpleNamespace(
        id=9_999,
        content=case.prompt,
        attachments=[],
        embeds=[],
        mentions=[],
        author=current_author,
        created_at=now,
        edited_at=None,
        reference=None,
        guild=guild,
        channel=channel,
    )
    if reply_chain:
        current.reference = SimpleNamespace(
            message_id=reply_chain[0].id,
            resolved=reply_chain[0],
            cached_message=reply_chain[0],
        )

    channel.set_messages([*recent, *reply_chain])
    fake_bot = _SyntheticBot(channel=channel)
    collector = MessageContextCollector(
        bot=fake_bot,
        channel_context_limit=template_collector.channel_context_limit,
        max_reply_chain_depth=template_collector.max_reply_chain_depth,
        max_linked_message_count=template_collector.max_linked_message_count,
        max_context_image_count=template_collector.max_context_image_count,
        anchor_context_per_side=template_collector.anchor_context_per_side,
    )
    timings: dict[str, int] = {}
    context_lines, image_urls, image_context_lines, _members = (
        await collector.build_message_context_with_members(
            current,
            timing_metrics=timings,
        )
    )
    return LiveBenchmarkMessageContext(
        context_lines=tuple(context_lines),
        image_attachment_urls=tuple(image_urls),
        image_context_lines=tuple(image_context_lines),
        timing_metrics=timings,
    )


def _build_message(
    spec: LiveBenchmarkDiscordMessage,
    *,
    message_id: int,
    now: datetime,
    author: Any,
) -> Any:
    return SimpleNamespace(
        id=message_id,
        content=spec.content,
        attachments=[],
        embeds=[],
        mentions=[],
        author=author,
        created_at=now - timedelta(minutes=spec.minutes_ago),
        edited_at=None,
        reference=None,
        guild=None,
        channel=None,
    )


class _SyntheticChannel:
    def __init__(self, *, channel_id: int, guild: Any) -> None:
        self.id = channel_id
        self.guild = guild
        self._messages: list[Any] = []

    def set_messages(self, messages: list[Any]) -> None:
        self._messages = sorted(
            messages,
            key=lambda message: (message.created_at, message.id),
        )
        for message in self._messages:
            message.channel = self
            message.guild = self.guild

    async def history(
        self,
        *,
        limit: int,
        before: Any | None = None,
        after: Any | None = None,
        oldest_first: bool,
    ):  # type: ignore[no-untyped-def]
        selected = list(self._messages)
        if before is not None:
            before_key = (before.created_at, before.id)
            selected = [
                item for item in selected if (item.created_at, item.id) < before_key
            ]
        if after is not None:
            after_key = (after.created_at, after.id)
            selected = [
                item for item in selected if (item.created_at, item.id) > after_key
            ]
        if not oldest_first:
            selected.reverse()
        for item in selected[:limit]:
            yield item

    async def fetch_message(self, message_id: int) -> Any:
        for message in self._messages:
            if message.id == message_id:
                return message
        raise LookupError(message_id)

    def permissions_for(self, _member: object) -> Any:
        return SimpleNamespace(view_channel=True, read_messages=True)


class _SyntheticBot:
    def __init__(self, *, channel: _SyntheticChannel) -> None:
        self._channel = channel
        self.cached_messages = list(channel._messages)

    def get_message(self, message_id: int) -> Any | None:
        return next(
            (message for message in self.cached_messages if message.id == message_id),
            None,
        )

    def get_channel(self, channel_id: int) -> _SyntheticChannel | None:
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id: int) -> _SyntheticChannel | None:
        return self.get_channel(channel_id)
