import unittest

from nycti.startup import compute_discord_start_backoff_seconds, is_retryable_discord_start_error


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
