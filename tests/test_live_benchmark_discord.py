from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.live_benchmark_discord import build_live_benchmark_message_context
from nycti.live_benchmarks import (
    LIVE_BENCHMARK_FIXTURE_NOW,
    load_live_benchmark_manifest,
)
from nycti.message_context import MessageContextCollector


class LiveBenchmarkDiscordContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_reply_chain_uses_production_context_collector(self) -> None:
        case = load_live_benchmark_manifest().get_case("fixture-discord-reply-time")
        template = MessageContextCollector(
            bot=SimpleNamespace(cached_messages=[]),
            channel_context_limit=12,
            max_reply_chain_depth=3,
            max_linked_message_count=3,
            max_context_image_count=3,
            anchor_context_per_side=1,
        )

        result = await build_live_benchmark_message_context(
            case,
            template_collector=template,
            now=LIVE_BENCHMARK_FIXTURE_NOW,
        )

        rendered = "\n".join(result.context_lines)
        self.assertIn("[reply depth 1]", rendered)
        self.assertIn("Lucis: Launch moved to Thursday at 8 PM.", rendered)
        self.assertIn("GTS81: Are we still launching Nightjar on Friday?", rendered)
        self.assertIn("benchmark: when is it?", rendered)
        self.assertEqual((), result.image_attachment_urls)
        self.assertIn("ctx_discord_ms", result.timing_metrics or {})

    async def test_empty_synthetic_context_does_no_collection_work(self) -> None:
        case = load_live_benchmark_manifest().get_case("fixture-quick-recursion")
        template = MessageContextCollector(
            bot=SimpleNamespace(cached_messages=[]),
            channel_context_limit=12,
            max_reply_chain_depth=3,
            max_linked_message_count=3,
            max_context_image_count=3,
            anchor_context_per_side=1,
        )

        result = await build_live_benchmark_message_context(
            case,
            template_collector=template,
            now=LIVE_BENCHMARK_FIXTURE_NOW,
        )

        self.assertEqual((), result.context_lines)
        self.assertEqual({}, result.timing_metrics)


if __name__ == "__main__":
    unittest.main()
