import unittest
from datetime import datetime, timedelta, timezone

from nycti.reminders.parsing import parse_remind_at


class ReminderParsingTests(unittest.TestCase):
    def test_parse_remind_at_supports_date_only_with_default_local_hour(self) -> None:
        parsed = parse_remind_at(
            "2026-03-25",
            now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.assumed_time)
        self.assertEqual(parsed.remind_at.hour, 9)
        self.assertEqual(parsed.remind_at.minute, 0)

    def test_parse_remind_at_supports_iso_datetime(self) -> None:
        parsed = parse_remind_at(
            "2026-03-25T15:30:00-07:00",
            now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertFalse(parsed.assumed_time)
        self.assertEqual(parsed.remind_at.utcoffset(), timedelta(hours=-7))
        self.assertEqual(parsed.remind_at.hour, 15)

    def test_parse_remind_at_assigns_local_timezone_to_naive_datetime(self) -> None:
        parsed = parse_remind_at(
            "2026-03-25 15:30:00",
            now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.remind_at.tzinfo, timezone.utc)

    def test_parse_remind_at_rejects_invalid_values(self) -> None:
        parsed = parse_remind_at(
            "next-ish week maybe",
            now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
