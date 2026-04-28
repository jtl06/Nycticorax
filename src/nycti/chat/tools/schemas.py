from __future__ import annotations

from collections.abc import Collection

WEB_SEARCH_TOOL_NAME = "web_search"
STOCK_QUOTE_TOOL_NAME = "stock_quote"
PRICE_HISTORY_TOOL_NAME = "price_history"
GET_CHANNEL_CONTEXT_TOOL_NAME = "get_channel_context"
IMAGE_SEARCH_TOOL_NAME = "image_search"
EXTRACT_URL_TOOL_NAME = "extract_url_content"
BROWSER_EXTRACT_TOOL_NAME = "browser_extract_content"
YOUTUBE_TRANSCRIPT_TOOL_NAME = "youtube_transcript"
CREATE_REMINDER_TOOL_NAME = "create_reminder"
SEND_CHANNEL_MESSAGE_TOOL_NAME = "send_channel_message"
UPDATE_PERSONAL_PROFILE_TOOL_NAME = "update_personal_profile"
PYTHON_EXEC_TOOL_NAME = "python_exec"


def build_chat_tools(enabled_names: Collection[str] | None = None) -> list[dict[str, object]]:
    selected_names = set(enabled_names) if enabled_names is not None else None
    tools = [
        {
            "type": "function",
            "function": {
                "name": WEB_SEARCH_TOOL_NAME,
                "description": (
                    "Search fresh public web info; prefer one focused query first. "
                    "Use for historical market benchmarks or dated reference facts instead of model memory."
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
                "name": STOCK_QUOTE_TOOL_NAME,
                "description": (
                    "Fetch latest quotes for up to 5 supported stocks, ETFs, indexes, or futures. "
                    "When the regular market is closed, this automatically adds Yahoo Finance pre/post-market fallback data when available."
                ),
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
                "description": (
                    "Fetch older Discord context when the default recent window is insufficient; "
                    "raw is smaller, summary is larger. Set expand=true only when exact longer wording is needed."
                ),
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
                        "expand": {
                            "type": "boolean",
                            "description": (
                                "Optional. False by default. Set true to use a wider per-message line cap "
                                "when longer quotes are needed."
                            ),
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
                "name": BROWSER_EXTRACT_TOOL_NAME,
                "description": (
                    "Extract page content using Chromium for JavaScript-heavy or blocked sites. "
                    "Use when normal URL extraction fails or returns thin content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The exact public URL to load in Chromium.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional focus query for selecting relevant extracted lines.",
                        },
                        "headed": {
                            "type": "boolean",
                            "description": (
                                "Optional. False by default. Set true only when explicitly needed and allowed by runtime."
                            ),
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": YOUTUBE_TRANSCRIPT_TOOL_NAME,
                "description": (
                    "Extract and efficiency-model summarize a transcript from a specific YouTube video URL. "
                    "Use this before generic URL extraction for YouTube summaries or transcript questions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The exact YouTube video URL.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional focus query for selecting relevant transcript chunks.",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": UPDATE_PERSONAL_PROFILE_TOOL_NAME,
                "description": (
                    "Update the calling user's compact profile note when durable personal context changed. "
                    "Use sparingly; only call when there is genuinely new long-term user information."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": (
                                "Optional focused durable profile note to evaluate. "
                                "If omitted, evaluate the current source message plus recent channel context."
                            ),
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PYTHON_EXEC_TOOL_NAME,
                "description": (
                    "Run a small Python calculation in a restricted sandbox. "
                    "Use for math, parsing, small data transforms, or table preparation; no imports/files/network."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Short Python code. Assign final value to `result` or print output.",
                        }
                    },
                    "required": ["code"],
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
    if selected_names is None:
        return tools
    return [
        tool
        for tool in tools
        if isinstance(tool.get("function"), dict)
        and isinstance(tool["function"].get("name"), str)
        and tool["function"]["name"] in selected_names
    ]
