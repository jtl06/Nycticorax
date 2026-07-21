from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from nycti.chat.action_confirmation import ActionConfirmationStore
from nycti.chat.run_state import AgentPermissions, ToolExecutionResult, ToolStatus
from nycti.chat.tools.actions import ActionToolMixin
from nycti.chat.tools.content import ContentToolMixin
from nycti.chat.tools.handlers import RegisteredToolHandlerMixin, ToolExecutionContext
from nycti.chat.tools.market import MarketToolMixin
from nycti.chat.tools.memory import MemoryToolMixin
from nycti.chat.tools.research import ResearchToolMixin
from nycti.chat.tools.registry import get_tool_spec
from nycti.chat.tools.registry import TOOL_SPECS
from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    DEEP_RESEARCH_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    REPORT_RESPONSE_ISSUE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)
from nycti.chat.tools.telemetry import ToolTelemetryMixin
from nycti.timing import elapsed_ms

if TYPE_CHECKING:
    import discord

    from nycti.browser import BrowserClient
    from nycti.channel_aliases import ChannelAliasService
    from nycti.chat.deep_research import CompositeDeepResearchService
    from nycti.db.session import Database
    from nycti.llm.client import OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient
    from nycti.yahoo import YahooFinanceClient
    from nycti.youtube import YouTubeTranscriptClient

MAX_CONCURRENT_DEEP_RESEARCH_CALLS = 2


class ChatToolExecutor(
    RegisteredToolHandlerMixin,
    ActionToolMixin,
    ContentToolMixin,
    MarketToolMixin,
    MemoryToolMixin,
    ResearchToolMixin,
    ToolTelemetryMixin,
):
    def __init__(
        self,
        *,
        database: Database,
        settings: object,
        llm_client: OpenAIClient,
        market_data_client: TwelveDataClient,
        tavily_client: TavilyClient,
        yahoo_finance_client: YahooFinanceClient | None = None,
        browser_client: BrowserClient | None = None,
        youtube_client: YouTubeTranscriptClient | None = None,
        memory_service: MemoryService,
        channel_alias_service: ChannelAliasService,
        reminder_service: ReminderService,
        deep_research_service: CompositeDeepResearchService | None = None,
        bot: discord.Client,
    ) -> None:
        self.database = database
        self.settings = settings
        self.llm_client = llm_client
        self.market_data_client = market_data_client
        self.yahoo_finance_client = yahoo_finance_client
        self.tavily_client = tavily_client
        self.browser_client = browser_client
        self.youtube_client = youtube_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self.deep_research_service = deep_research_service
        self.bot = bot
        self.deep_research_semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_DEEP_RESEARCH_CALLS
        )
        self.action_confirmations = ActionConfirmationStore()
        self._claimed_action_keys: set[str] = set()

    def available_tool_names(
        self,
        *,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
    ) -> frozenset[str]:
        """Return tools backed by a configured runtime capability for this request."""

        available = set(TOOL_SPECS)
        tavily_available = bool(getattr(self.tavily_client, "api_key", True))
        market_available = bool(getattr(self.market_data_client, "api_key", True))
        browser_available = self.browser_client is not None and bool(
            getattr(self.browser_client, "enabled", True)
        )
        youtube_available = self.youtube_client is not None and bool(
            getattr(self.youtube_client, "enabled", True)
        )
        if not tavily_available:
            available.difference_update({WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME})
        if not tavily_available and not browser_available:
            available.discard(EXTRACT_URL_TOOL_NAME)
        if not browser_available:
            available.discard(BROWSER_EXTRACT_TOOL_NAME)
        if not youtube_available:
            available.discard(YOUTUBE_TRANSCRIPT_TOOL_NAME)
        if not getattr(self.settings, "python_tool_enabled", False):
            available.discard(PYTHON_EXEC_TOOL_NAME)
        if not market_available:
            available.difference_update({STOCK_QUOTE_TOOL_NAME, PRICE_HISTORY_TOOL_NAME})
        if self.yahoo_finance_client is None:
            available.discard(ANNUAL_PERFORMANCE_TOOL_NAME)
        if self.deep_research_service is None:
            available.discard(DEEP_RESEARCH_TOOL_NAME)
        if guild_id is None or channel_id is None or source_message_id is None:
            available.difference_update(
                {GET_CHANNEL_CONTEXT_TOOL_NAME, REPORT_RESPONSE_ISSUE_TOOL_NAME}
            )
        if guild_id is None or channel_id is None:
            available.difference_update(
                {CREATE_REMINDER_TOOL_NAME, SEND_CHANNEL_MESSAGE_TOOL_NAME}
            )
        return frozenset(available)

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions | None = None,
        run_id: str = "",
        step_index: int = 0,
    ) -> ToolExecutionResult:
        spec = get_tool_spec(tool_name)
        if spec is None:
            return await self._finalize_tool_call(
                tool_name=tool_name,
                execution=ToolExecutionResult(
                    content=f"Unknown tool `{tool_name}`.",
                    status=ToolStatus.ERROR,
                ),
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                started_at=time.perf_counter(),
                defer_telemetry=bool(run_id),
            )
        effective_permissions = permissions or AgentPermissions()
        context = ToolExecutionContext(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
            permissions=effective_permissions,
            run_id=run_id,
            step_index=step_index,
        )
        handler = getattr(self, spec.handler_name)
        started_at = time.perf_counter()
        try:
            execution = await asyncio.wait_for(
                handler(arguments, context),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError:
            execution = ToolExecutionResult(
                content=f"{tool_name} failed because it exceeded its {spec.timeout_seconds:g}s timeout.",
                status=ToolStatus.ERROR,
                metrics={"tool_timeout_count": 1},
                retryable=True,
            )
        execution.content = execution.content.strip()
        return await self._finalize_tool_call(
            tool_name=tool_name,
            execution=execution,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            started_at=started_at,
            defer_telemetry=bool(run_id),
        )

    async def _finalize_tool_call(
        self,
        *,
        tool_name: str,
        execution: ToolExecutionResult,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        started_at: float,
        defer_telemetry: bool,
    ) -> ToolExecutionResult:
        if not defer_telemetry:
            await self._record_tool_call_event(
                tool_name=tool_name,
                status=execution.status,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                latency_ms=elapsed_ms(started_at),
            )
        return execution
