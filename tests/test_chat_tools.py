import unittest

from nycti.chat.tools.parsing import (
    parse_create_reminder_arguments,
    parse_send_channel_message_arguments,
    parse_tool_query_argument,
)
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    build_chat_tools,
)


class ChatToolParsingTests(unittest.TestCase):
    def test_parse_tool_query_argument_returns_query_string(self) -> None:
        self.assertEqual(
            parse_tool_query_argument('{"query":"latest nvda earnings"}'),
            "latest nvda earnings",
        )

    def test_parse_create_reminder_arguments_requires_both_fields(self) -> None:
        payload = parse_create_reminder_arguments('{"message":"check NVDA","remind_at":"2026-03-22"}')
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.message, "check NVDA")
        self.assertEqual(payload.remind_at, "2026-03-22")
        self.assertIsNone(parse_create_reminder_arguments('{"message":"check NVDA"}'))

    def test_parse_send_channel_message_arguments_requires_both_fields(self) -> None:
        payload = parse_send_channel_message_arguments('{"channel":"alerts","message":"deploy live"}')
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.channel, "alerts")
        self.assertEqual(payload.message, "deploy live")
        self.assertIsNone(parse_send_channel_message_arguments('{"channel":"alerts"}'))


class ChatToolSchemaTests(unittest.TestCase):
    def test_build_chat_tools_returns_expected_tool_names(self) -> None:
        names = [
            tool["function"]["name"]
            for tool in build_chat_tools()
            if isinstance(tool.get("function"), dict)
        ]
        self.assertEqual(
            names,
            [
                WEB_SEARCH_TOOL_NAME,
                CREATE_REMINDER_TOOL_NAME,
                SEND_CHANNEL_MESSAGE_TOOL_NAME,
            ],
        )


if __name__ == "__main__":
    unittest.main()
