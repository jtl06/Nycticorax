import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from nycti.startup import compute_discord_start_backoff_seconds, is_retryable_discord_start_error


class MaintenanceModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_does_not_open_database_or_start_bot(self):
        from nycti.main import run

        with patch("nycti.main.Settings.from_env", return_value=SimpleNamespace(maintenance_mode=True)), \
                patch("nycti.main.Database") as database, patch("nycti.main.build_nycti_bot") as bot:
            task = asyncio.create_task(run())
            await asyncio.sleep(0)
            database.assert_not_called()
            bot.assert_not_called()
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class MainStartupRetryTests(unittest.TestCase):
    def test_compute_backoff_grows_then_caps(self) -> None:
        self.assertEqual(compute_discord_start_backoff_seconds(1), 15)
        self.assertEqual(compute_discord_start_backoff_seconds(2), 30)
        self.assertEqual(compute_discord_start_backoff_seconds(3), 60)
        self.assertEqual(compute_discord_start_backoff_seconds(10), 300)

    def test_retryable_discord_start_error_detects_cloudflare_1015(self) -> None:
        class FakeDiscordHTTPException(Exception):
            def __init__(self) -> None:
                self.status = 429

            def __str__(self) -> str:
                return "Access denied | discord.com used Cloudflare to restrict access | Error 1015"

        exc = FakeDiscordHTTPException()
        self.assertTrue(is_retryable_discord_start_error(exc))

    def test_retryable_discord_start_error_rejects_non_http_exception(self) -> None:
        self.assertFalse(is_retryable_discord_start_error(RuntimeError("boom")))


if __name__ == "__main__":
    unittest.main()
