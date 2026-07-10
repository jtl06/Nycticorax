from __future__ import annotations

from types import SimpleNamespace
import unittest

from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    DEEP_RESEARCH_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)


class RuntimeToolAvailabilityTests(unittest.TestCase):
    def test_unconfigured_providers_are_not_exposed(self) -> None:
        executor = _executor(
            tavily_key=None,
            market_key=None,
            python_enabled=False,
        )

        names = executor.available_tool_names(
            guild_id=1,
            channel_id=2,
            source_message_id=3,
        )

        self.assertTrue({CREATE_REMINDER_TOOL_NAME, SEND_CHANNEL_MESSAGE_TOOL_NAME} <= names)
        self.assertIn(GET_CHANNEL_CONTEXT_TOOL_NAME, names)
        self.assertIn(MEMORY_SEARCH_TOOL_NAME, names)
        self.assertTrue(
            {
                WEB_SEARCH_TOOL_NAME,
                IMAGE_SEARCH_TOOL_NAME,
                EXTRACT_URL_TOOL_NAME,
                BROWSER_EXTRACT_TOOL_NAME,
                YOUTUBE_TRANSCRIPT_TOOL_NAME,
                STOCK_QUOTE_TOOL_NAME,
                PRICE_HISTORY_TOOL_NAME,
                PYTHON_EXEC_TOOL_NAME,
                DEEP_RESEARCH_TOOL_NAME,
            }.isdisjoint(names)
        )

    def test_request_context_removes_only_context_bound_tools(self) -> None:
        executor = _executor(tavily_key="key", market_key="key", python_enabled=True)

        names = executor.available_tool_names(
            guild_id=None,
            channel_id=None,
            source_message_id=None,
        )

        self.assertNotIn(GET_CHANNEL_CONTEXT_TOOL_NAME, names)
        self.assertNotIn(CREATE_REMINDER_TOOL_NAME, names)
        self.assertNotIn(SEND_CHANNEL_MESSAGE_TOOL_NAME, names)
        self.assertIn(WEB_SEARCH_TOOL_NAME, names)
        self.assertIn(STOCK_QUOTE_TOOL_NAME, names)
        self.assertIn(PYTHON_EXEC_TOOL_NAME, names)


def _executor(
    *,
    tavily_key: str | None,
    market_key: str | None,
    python_enabled: bool,
) -> ChatToolExecutor:
    return ChatToolExecutor(
        database=SimpleNamespace(),
        settings=SimpleNamespace(python_tool_enabled=python_enabled),
        llm_client=SimpleNamespace(),
        market_data_client=SimpleNamespace(api_key=market_key),
        tavily_client=SimpleNamespace(api_key=tavily_key),
        yahoo_finance_client=None,
        browser_client=None,
        youtube_client=None,
        memory_service=SimpleNamespace(),
        channel_alias_service=SimpleNamespace(),
        reminder_service=SimpleNamespace(),
        deep_research_service=None,
        bot=SimpleNamespace(),
    )


if __name__ == "__main__":
    unittest.main()
