from __future__ import annotations

WEB_SEARCH_TOOL_NAME = "web_search"
STOCK_QUOTE_TOOL_NAME = "stock_quote"
PRICE_HISTORY_TOOL_NAME = "price_history"
GET_CHANNEL_CONTEXT_TOOL_NAME = "get_channel_context"
IMAGE_SEARCH_TOOL_NAME = "image_search"
EXTRACT_URL_TOOL_NAME = "extract_url_content"
CREATE_REMINDER_TOOL_NAME = "create_reminder"
SEND_CHANNEL_MESSAGE_TOOL_NAME = "send_channel_message"


def build_chat_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": WEB_SEARCH_TOOL_NAME,
                "description": "Search fresh public web info; prefer one focused query first.",
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
                "name": STOCK_QUOTE_TOOL_NAME,
                "description": "Fetch latest quotes for up to 5 supported stocks, ETFs, indexes, or futures.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "One symbol or a comma-separated list of up to 5 symbols, like AAPL, NVDA, SPY, SPX, or ES.",
                        },
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 5,
                            "description": "Optional explicit list of up to 5 symbols to quote in one tool call.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PRICE_HISTORY_TOOL_NAME,
                "description": "Fetch recent historical candles for one supported market symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "One supported market symbol like SPY, AAPL, or NVDA.",
                        },
                        "interval": {
                            "type": "string",
                            "description": "Optional candle interval like 1day, 1week, 1month, 1h, or 5min. Defaults to 1day.",
                        },
                        "outputsize": {
                            "type": "integer",
                            "description": "Optional number of candles to return, from 1 to 30. Defaults to 5.",
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Optional inclusive start date or datetime for the series window.",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "Optional inclusive end date or datetime for the series window.",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": GET_CHANNEL_CONTEXT_TOOL_NAME,
                "description": "Fetch older Discord context when recent context is insufficient; prefer summary for chat/history summaries, raw for exact wording.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["raw", "summary"],
                            "description": "raw returns older messages directly; summary returns a cheap-model summary of a larger older window.",
                        },
                        "multiplier": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3,
                            "description": "How much older context to fetch, from 1 to 3. Defaults to 1.",
                        },
                    },
                    "required": ["mode"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": IMAGE_SEARCH_TOOL_NAME,
                "description": "Search for direct image URLs when the user wants to see an example.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The focused image search query to run.",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": EXTRACT_URL_TOOL_NAME,
                "description": "Extract readable content from a specific public URL; optional query narrows focus.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The exact public URL to extract.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional focus query for the extraction.",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CREATE_REMINDER_TOOL_NAME,
                "description": "Create a future reminder for the current user in this channel.",
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
                "description": "Send a message to another channel only when explicitly requested.",
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
