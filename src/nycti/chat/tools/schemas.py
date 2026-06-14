from __future__ import annotations

from collections.abc import Collection

WEB_SEARCH_TOOL_NAME = "web"
STOCK_QUOTE_TOOL_NAME = "quote"
PRICE_HISTORY_TOOL_NAME = "price_hist"
ANNUAL_PERFORMANCE_TOOL_NAME = "annual_perf"
GET_CHANNEL_CONTEXT_TOOL_NAME = "channel_ctx"
IMAGE_SEARCH_TOOL_NAME = "img_search"
EXTRACT_URL_TOOL_NAME = "url_extract"
BROWSER_EXTRACT_TOOL_NAME = "browser_extract"
YOUTUBE_TRANSCRIPT_TOOL_NAME = "yt_transcript"
CREATE_REMINDER_TOOL_NAME = "reminder"
SEND_CHANNEL_MESSAGE_TOOL_NAME = "send_msg"
PYTHON_EXEC_TOOL_NAME = "python"


def build_chat_tools(enabled_names: Collection[str] | None = None) -> list[dict[str, object]]:
    from nycti.chat.tools.registry import build_registered_tools

    return build_registered_tools(enabled_names)
