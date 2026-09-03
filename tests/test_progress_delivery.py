from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class ProgressDeliveryTests(unittest.TestCase):
    def test_final_edit_failure_falls_back_without_retaining_stale_bar(self) -> None:
        from nycti.bot import NyctiBot

        fallback_message = object()
        progress_message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("gone")))
        source_message = SimpleNamespace(
            reply=AsyncMock(return_value=fallback_message),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        progress = SimpleNamespace(mark_resolved=Mock())
        bot = object.__new__(NyctiBot)

        with patch("nycti.bot.discord.NotFound", RuntimeError):
            sent = asyncio.run(
                bot._send_message_reply_chunks(
                    source_message,
                    "Final answer.",
                    progress_message=progress_message,
                    progress=progress,
                )
            )

        self.assertEqual((fallback_message,), sent.messages)
        self.assertTrue(sent.complete)
        source_message.reply.assert_awaited_once()
        progress.mark_resolved.assert_not_called()

    def test_final_edit_429_opens_cooldown_without_fallback_send(self) -> None:
        from nycti.bot import NyctiBot
        from nycti.discord.rate_limits import DiscordRateLimitCircuitBreaker

        class FakeDiscordHTTPException(Exception):
            status = 429

        breaker = DiscordRateLimitCircuitBreaker(default_cooldown_seconds=60)
        progress_message = SimpleNamespace(
            edit=AsyncMock(side_effect=FakeDiscordHTTPException("globally blocked"))
        )
        source_message = SimpleNamespace(
            reply=AsyncMock(),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        progress = SimpleNamespace(mark_resolved=Mock())
        bot = object.__new__(NyctiBot)

        with (
            patch("nycti.bot.discord.HTTPException", FakeDiscordHTTPException),
            patch("nycti.discord.reply_delivery.DISCORD_OUTBOUND_CIRCUIT_BREAKER", breaker),
        ):
            sent = asyncio.run(
                bot._send_message_reply_chunks(
                    source_message,
                    "Final answer.",
                    progress_message=progress_message,
                    progress=progress,
                )
            )

        self.assertEqual((), sent.messages)
        self.assertFalse(sent.complete)
        self.assertTrue(breaker.is_open)
        progress_message.edit.assert_awaited_once()
        source_message.reply.assert_not_awaited()
        progress.mark_resolved.assert_not_called()

    def test_cancel_edit_failure_falls_back_without_retaining_stale_bar(self) -> None:
        from nycti.bot import _edit_progress_or_reply

        fallback_message = object()
        progress_message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("gone")))
        source_message = SimpleNamespace(reply=AsyncMock(return_value=fallback_message))
        progress = SimpleNamespace(mark_resolved=Mock())

        with patch("nycti.bot.discord.NotFound", RuntimeError):
            sent = asyncio.run(
                _edit_progress_or_reply(
                    source_message,
                    progress_message,
                    "Cancelled your active request.",
                    progress=progress,
                )
            )

        self.assertIs(fallback_message, sent)
        source_message.reply.assert_awaited_once()
        progress.mark_resolved.assert_not_called()

    def test_cancel_edit_429_does_not_immediately_retry_as_reply(self) -> None:
        from nycti.bot import _edit_progress_or_reply
        from nycti.discord.rate_limits import DiscordRateLimitCircuitBreaker

        class FakeDiscordHTTPException(Exception):
            status = 429

        breaker = DiscordRateLimitCircuitBreaker(default_cooldown_seconds=60)
        progress_message = SimpleNamespace(
            edit=AsyncMock(side_effect=FakeDiscordHTTPException("globally blocked"))
        )
        source_message = SimpleNamespace(reply=AsyncMock())

        with (
            patch("nycti.bot.discord.HTTPException", FakeDiscordHTTPException),
            patch("nycti.bot.DISCORD_OUTBOUND_CIRCUIT_BREAKER", breaker),
        ):
            sent = asyncio.run(
                _edit_progress_or_reply(
                    source_message,
                    progress_message,
                    "Cancelled your active request.",
                )
            )

        self.assertIsNone(sent)
        self.assertTrue(breaker.is_open)
        source_message.reply.assert_not_awaited()

    def test_attachment_delete_failure_leaves_bar_eligible_for_cleanup_retry(self) -> None:
        from nycti.bot import NyctiBot

        progress_message = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("busy")))
        source_message = SimpleNamespace(
            reply=AsyncMock(return_value=object()),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        progress = SimpleNamespace(mark_resolved=Mock())
        extraction = SimpleNamespace(
            text="Rendered table.",
            images=[SimpleNamespace(data=b"png", filename="table.png")],
        )
        bot = object.__new__(NyctiBot)

        with (
            patch("nycti.bot.discord.NotFound", RuntimeError),
            patch(
                "nycti.discord.reply_delivery.extract_markdown_tables_as_images",
                return_value=extraction,
            ),
        ):
            asyncio.run(
                bot._send_message_reply_chunks(
                    source_message,
                    "| a |\n| - |\n| b |",
                    progress_message=progress_message,
                    progress=progress,
                )
            )

        progress_message.delete.assert_awaited_once()
        source_message.reply.assert_awaited_once()
        progress.mark_resolved.assert_not_called()

    def test_later_chunk_429_reports_partial_delivery(self) -> None:
        from nycti.bot import NyctiBot
        from nycti.discord.rate_limits import DiscordRateLimitCircuitBreaker

        class FakeDiscordHTTPException(Exception):
            status = 429

        breaker = DiscordRateLimitCircuitBreaker(default_cooldown_seconds=60)
        first_message = SimpleNamespace(id=101)
        source_message = SimpleNamespace(
            reply=AsyncMock(return_value=first_message),
            channel=SimpleNamespace(
                send=AsyncMock(side_effect=FakeDiscordHTTPException("globally blocked"))
            ),
        )
        bot = object.__new__(NyctiBot)

        with (
            patch("nycti.bot.discord.HTTPException", FakeDiscordHTTPException),
            patch("nycti.discord.reply_delivery.DISCORD_OUTBOUND_CIRCUIT_BREAKER", breaker),
            patch(
                "nycti.discord.reply_delivery.split_message_chunks",
                return_value=["first", "second"],
            ),
        ):
            delivery = asyncio.run(
                bot._send_message_reply_chunks(source_message, "full answer")
            )

        self.assertEqual((first_message,), delivery.messages)
        self.assertFalse(delivery.complete)
        self.assertTrue(breaker.is_open)


if __name__ == "__main__":
    unittest.main()
