import unittest

from nycti.formatting import format_ping_message


class BotUtilitiesTests(unittest.TestCase):
    def test_format_ping_message_rounds_to_milliseconds(self) -> None:
        self.assertEqual(format_ping_message(0.1234), "Pong! `123 ms`")

    def test_format_ping_message_clamps_negative_latency(self) -> None:
        self.assertEqual(format_ping_message(-1.0), "Pong! `0 ms`")


if __name__ == "__main__":
    unittest.main()
