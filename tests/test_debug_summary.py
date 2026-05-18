import unittest
from datetime import datetime, timedelta, timezone

from nycti.debug_summary import _posted_recently


class DebugSummaryTests(unittest.TestCase):
    def test_posted_recently_handles_missing_and_invalid_values(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

        self.assertFalse(_posted_recently(None, now=now))
        self.assertFalse(_posted_recently("not-a-date", now=now))

    def test_posted_recently_uses_daily_window(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(_posted_recently((now - timedelta(hours=23)).isoformat(), now=now))
        self.assertFalse(_posted_recently((now - timedelta(hours=25)).isoformat(), now=now))


if __name__ == "__main__":
    unittest.main()
