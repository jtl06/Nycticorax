from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.bot import NyctiBot


class ReminderDeliverySecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_allows_only_the_intended_user_mention(self) -> None:
        channel = _Channel()
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel)
        reminder = SimpleNamespace(
            id=1,
            guild_id=7,
            channel_id=8,
            user_id=9,
            source_message_id=None,
            reminder_text="Review this @everyone <@&123> <@456>",
            remind_at=SimpleNamespace(timestamp=lambda: 1_800_000_000),
        )

        delivered = await NyctiBot._deliver_reminder(bot, reminder)  # type: ignore[arg-type]

        self.assertTrue(delivered)
        allowed = channel.kwargs["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertEqual([9], [user.id for user in allowed.users])


class _Channel:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def send(self, _content: str, **kwargs: object) -> None:
        self.kwargs = kwargs


if __name__ == "__main__":
    unittest.main()
