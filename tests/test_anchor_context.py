import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from nycti.message_context_source import DiscordMessageContextSource


class Channel:
    def __init__(self, channel_id=10):
        self.id = channel_id
        self.guild = SimpleNamespace(id=1)
        self.messages = [SimpleNamespace(id=i, channel=self, guild=self.guild,
                         created_at=datetime.now(timezone.utc)) for i in range(1, 21)]
        self.calls = []

    async def history(self, *, limit, before=None, after=None, around=None, oldest_first=False):
        self.calls.append((before, after, around))
        rows = self.messages
        if around is not None:
            n = limit // 2
            rows = ([m for m in rows if m.id < around.id][-n:]
                    + [m for m in rows if m.id == around.id]
                    + [m for m in rows if m.id > around.id][:n])
        elif before is not None:
            rows = [m for m in rows if m.id < before.id]
        elif after is not None:
            rows = [m for m in rows if m.id > after.id]
        if not oldest_first:
            rows = list(reversed(rows))
        for row in rows[:limit]:
            yield row


def source(cache=()):
    return DiscordMessageContextSource(bot=SimpleNamespace(cached_messages=list(cache)),
        channel_context_limit=12, max_reply_chain_depth=3,
        max_linked_message_count=3, anchor_context_per_side=1)


class AnchorContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_anchor_is_one_batched_request(self):
        channel = Channel()
        result = await source()._collect_anchor_context_messages(
            channel.messages[-1], anchor_messages=[channel.messages[4]])
        self.assertEqual([m.id for m in result], [4, 6])
        self.assertEqual(len(channel.calls), 1)
        self.assertIsNotNone(channel.calls[0][2])

    async def test_warm_cache_avoids_history(self):
        channel = Channel()
        result = await source(channel.messages)._collect_anchor_context_messages(
            channel.messages[-1], anchor_messages=[channel.messages[4]])
        self.assertEqual([m.id for m in result], [4, 6])
        self.assertEqual(channel.calls, [])

    async def test_overlapping_anchors_reuse_fetched_window(self):
        channel = Channel()
        result = await source()._collect_anchor_context_messages(
            channel.messages[-1], anchor_messages=[channel.messages[4], channel.messages[5]])
        self.assertEqual([m.id for m in result], [4, 7])
        self.assertEqual(len(channel.calls), 1)

    async def test_recent_history_can_supply_neighbors(self):
        channel = Channel()
        result = await source()._collect_anchor_context_messages(
            channel.messages[-1], anchor_messages=[channel.messages[4]],
            known_messages=channel.messages[3:6])
        self.assertEqual([m.id for m in result], [4, 6])
        self.assertEqual(channel.calls, [])

    async def test_future_messages_and_other_channel_cache_not_included(self):
        channel, other = Channel(), Channel(11)
        result = await source(other.messages)._collect_anchor_context_messages(
            channel.messages[5], anchor_messages=[channel.messages[4]])
        self.assertEqual([m.id for m in result], [4])
        self.assertEqual(len(channel.calls), 1)
        self.assertTrue(all(m.channel is channel for m in result))

    async def test_missing_anchor_in_sparse_cache_requires_fetch(self):
        channel = Channel()
        await source([channel.messages[0], channel.messages[-2]])._collect_anchor_context_messages(
            channel.messages[-1], anchor_messages=[channel.messages[4]])
        self.assertEqual(len(channel.calls), 1)

    async def test_parallelism_bounded_and_cancelled_requests_are_drained(self):
        active, peak = 0, 0
        two_started = asyncio.Event()

        class WaitingChannel(Channel):
            async def history(self, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 2:
                    two_started.set()
                try:
                    await asyncio.Event().wait()
                    yield self.messages[0]
                finally:
                    active -= 1

        channels = [WaitingChannel(i) for i in range(3)]
        for index, channel in enumerate(channels):
            for item in channel.messages:
                item.id += index * 100
        current = SimpleNamespace(id=1000, channel=channels[0])
        task = asyncio.create_task(source()._collect_anchor_context_messages(
            current, anchor_messages=[c.messages[4] for c in channels]))
        try:
            await asyncio.wait_for(two_started.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
