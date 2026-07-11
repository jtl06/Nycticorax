from __future__ import annotations

import asyncio
import unittest

from nycti.discord.progress import (
    DiscordResponseProgress,
    ResponseProgressPhase,
    render_response_progress,
)


class DiscordProgressRenderingTests(unittest.TestCase):
    def test_phases_render_fixed_monotonic_ten_cell_milestones(self) -> None:
        expected = (
            (ResponseProgressPhase.CONTEXT, 1, "Reading context"),
            (ResponseProgressPhase.MODEL, 3, "Thinking"),
            (ResponseProgressPhase.TOOLS, 6, "Checking information"),
            (ResponseProgressPhase.COMPOSING, 8, "Composing response"),
            (ResponseProgressPhase.DELIVERING, 9, "Preparing reply"),
        )

        for phase, filled, label in expected:
            with self.subTest(phase=phase):
                rendered = render_response_progress(phase)
                bar = rendered.split("`")[1]
                self.assertEqual(10, len(bar))
                self.assertEqual(filled, bar.count("█"))
                self.assertEqual(10 - filled, bar.count("░"))
                self.assertIn(f"{filled * 10}%", rendered)
                self.assertIn(label, rendered)
                self.assertNotIn("—", rendered)


class DiscordResponseProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_request_posts_nothing(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(source, delay_seconds=60).start()

        message = await progress.claim()

        self.assertIsNone(message)
        self.assertEqual([], source.replies)
        self.assertFalse(progress.is_running)

    async def test_delayed_message_uses_latest_phase_and_never_regresses(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(
            source,
            delay_seconds=0,
            debounce_seconds=0,
        ).start()
        await progress.advance(ResponseProgressPhase.TOOLS)
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        self.assertEqual(1, len(source.replies))
        self.assertIn("60%", source.replies[0])
        await progress.advance(ResponseProgressPhase.MODEL)
        self.assertEqual(ResponseProgressPhase.TOOLS, progress.phase)

        await progress.advance(ResponseProgressPhase.COMPOSING)
        await asyncio.wait_for(source.progress_message.edit_attempted.wait(), timeout=1)
        message = await progress.claim()

        self.assertIs(source.progress_message, message)
        self.assertEqual(1, len(source.progress_message.edits))
        self.assertIn("80%", source.progress_message.edits[0])
        self.assertEqual(1, len(source.replies))
        self.assertFalse(progress.is_running)

    async def test_rapid_updates_are_debounced_into_latest_phase(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(
            source,
            delay_seconds=0,
            debounce_seconds=0.03,
        ).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        await progress.advance(ResponseProgressPhase.MODEL)
        await progress.advance(ResponseProgressPhase.TOOLS)
        await progress.advance(ResponseProgressPhase.COMPOSING)
        await asyncio.wait_for(source.progress_message.edit_attempted.wait(), timeout=1)
        await asyncio.sleep(0.01)
        await progress.claim()

        self.assertEqual(1, len(source.progress_message.edits))
        self.assertIn("80%", source.progress_message.edits[0])

    async def test_reply_failure_is_fail_open(self) -> None:
        source = _FakeSourceMessage(fail_reply=True)
        progress = DiscordResponseProgress(source, delay_seconds=0).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        message = await progress.claim()

        self.assertIsNone(message)
        self.assertFalse(progress.is_running)

    async def test_edit_failure_is_fail_open(self) -> None:
        source = _FakeSourceMessage(fail_edit=True)
        progress = DiscordResponseProgress(
            source,
            delay_seconds=0,
            debounce_seconds=0,
        ).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        await progress.advance(ResponseProgressPhase.MODEL)
        await asyncio.wait_for(source.progress_message.edit_attempted.wait(), timeout=1)
        message = await progress.claim()

        self.assertIs(source.progress_message, message)
        self.assertEqual([], source.progress_message.edits)
        self.assertFalse(progress.is_running)

    async def test_discard_stops_worker_and_deletes_posted_message(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(source, delay_seconds=0).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        await progress.discard()

        self.assertTrue(source.progress_message.deleted)
        self.assertFalse(progress.is_running)

    async def test_claimed_message_is_deleted_until_final_reply_replaces_it(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(source, delay_seconds=0).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        self.assertIs(source.progress_message, await progress.claim())
        await progress.discard()

        self.assertTrue(source.progress_message.deleted)

    async def test_replaced_message_is_retained_during_cleanup(self) -> None:
        source = _FakeSourceMessage()
        progress = DiscordResponseProgress(source, delay_seconds=0).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)

        self.assertIs(source.progress_message, await progress.claim())
        progress.mark_resolved()
        await progress.discard()

        self.assertFalse(source.progress_message.deleted)

    async def test_cancelled_claim_does_not_leave_worker_running(self) -> None:
        edit_release = asyncio.Event()
        source = _FakeSourceMessage(edit_release=edit_release)
        progress = DiscordResponseProgress(
            source,
            delay_seconds=0,
            debounce_seconds=0,
        ).start()
        await asyncio.wait_for(source.reply_attempted.wait(), timeout=1)
        await progress.advance(ResponseProgressPhase.MODEL)
        await asyncio.wait_for(source.progress_message.edit_attempted.wait(), timeout=1)

        claim_task = asyncio.create_task(progress.claim())
        await asyncio.sleep(0)
        claim_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await claim_task

        await progress.discard()

        self.assertFalse(progress.is_running)
        self.assertTrue(source.progress_message.deleted)


class _FakeProgressMessage:
    def __init__(
        self,
        *,
        fail_edit: bool = False,
        edit_release: asyncio.Event | None = None,
    ) -> None:
        self.fail_edit = fail_edit
        self.edit_release = edit_release
        self.edits: list[str] = []
        self.edit_attempted = asyncio.Event()
        self.deleted = False

    async def edit(self, *, content: str) -> _FakeProgressMessage:
        self.edit_attempted.set()
        if self.edit_release is not None:
            await self.edit_release.wait()
        if self.fail_edit:
            raise RuntimeError("edit unavailable")
        self.edits.append(content)
        return self

    async def delete(self) -> None:
        self.deleted = True


class _FakeSourceMessage:
    def __init__(
        self,
        *,
        fail_reply: bool = False,
        fail_edit: bool = False,
        edit_release: asyncio.Event | None = None,
    ) -> None:
        self.fail_reply = fail_reply
        self.replies: list[str] = []
        self.reply_attempted = asyncio.Event()
        self.progress_message = _FakeProgressMessage(
            fail_edit=fail_edit,
            edit_release=edit_release,
        )

    async def reply(
        self,
        content: str,
        *,
        mention_author: bool,
    ) -> _FakeProgressMessage:
        self.reply_attempted.set()
        if self.fail_reply:
            raise RuntimeError("reply unavailable")
        if mention_author:
            raise AssertionError("Progress replies must not mention the author")
        self.replies.append(content)
        return self.progress_message


if __name__ == "__main__":
    unittest.main()
