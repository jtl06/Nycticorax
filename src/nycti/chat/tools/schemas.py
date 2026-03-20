from __future__ import annotations

WEB_SEARCH_TOOL_NAME = "web_search"
CREATE_REMINDER_TOOL_NAME = "create_reminder"
SEND_CHANNEL_MESSAGE_TOOL_NAME = "send_channel_message"


def build_chat_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": WEB_SEARCH_TOOL_NAME,
                "description": (
                    "Search the web for fresh public information and source snippets. "
                    "Prefer one comprehensive query first. Only issue another search if earlier results are insufficient or conflicting."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The focused web search query to run.",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CREATE_REMINDER_TOOL_NAME,
                "description": (
                    "Create a future reminder for the current user in this channel. "
                    "Use this when the user asks to be reminded on a specific date or time. "
                    "Prefer ISO 8601 date-times with timezone offsets. Date-only values are allowed and default to 09:00 local time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The short reminder text to send later.",
                        },
                        "remind_at": {
                            "type": "string",
                            "description": (
                                "When to send the reminder. Use an ISO 8601 local date or date-time, "
                                "for example 2026-03-22 or 2026-03-22T15:30:00-07:00."
                            ),
                        },
                    },
                    "required": ["message", "remind_at"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": SEND_CHANNEL_MESSAGE_TOOL_NAME,
                "description": (
                    "Send a message into another channel in the current Discord server. "
                    "Use a configured channel alias or a numeric channel ID. "
                    "Only use this when the user explicitly wants you to post somewhere else."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {
                            "type": "string",
                            "description": "Known channel alias or numeric channel ID.",
                        },
                        "message": {
                            "type": "string",
                            "description": "The message to send into that channel.",
                        },
                    },
                    "required": ["channel", "message"],
                },
            },
        },
    ]
