from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from nycti.chat.run_state import AgentPermissions
from nycti.chat.tools.actions import ActionToolMixin
from nycti.chat.tools.content import ContentToolMixin
from nycti.chat.tools.handlers import RegisteredToolHandlerMixin, ToolExecutionContext
from nycti.chat.tools.market import MarketToolMixin
from nycti.chat.tools.registry import get_tool_spec
from nycti.chat.tools.telemetry import ToolTelemetryMixin
from nycti.timing import elapsed_ms

if TYPE_CHECKING:
    import discord

    from nycti.browser import BrowserClient
    from nycti.channel_aliases import ChannelAliasService
    from nycti.db.session import Database
    from nycti.llm.client import OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient
    from nycti.yahoo import YahooFinanceClient
    from nycti.youtube import YouTubeTranscriptClient


class ChatToolExecutor(
    RegisteredToolHandlerMixin,
    ActionToolMixin,
    ContentToolMixin,
    MarketToolMixin,
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
        self.bot = bot
        self._claimed_action_keys: set[str] = set()

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
    ) -> tuple[str, dict[str, int | str]]:
        spec = get_tool_spec(tool_name)
        if spec is None:
            return await self._finalize_tool_call(
                tool_name=tool_name,
                result=f"Unknown tool `{tool_name}`.",
                metrics={},
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                started_at=time.perf_counter(),
                defer_telemetry=bool(run_id),
            )
        effective_permissions = permissions or AgentPermissions()
        if spec.permission_flag and not getattr(effective_permissions, spec.permission_flag):
            return await self._finalize_tool_call(
                tool_name=tool_name,
                result=f"{tool_name} failed because this action was not authorized for the current request.",
                metrics={"unauthorized_action_count": 1},
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                started_at=time.perf_counter(),
                defer_telemetry=bool(run_id),
            )

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
            result, metrics = await asyncio.wait_for(
                handler(arguments, context),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError:
            result = f"{tool_name} failed because it exceeded its {spec.timeout_seconds:g}s timeout."
            metrics = {"tool_timeout_count": 1}
        result = result.strip()
        return await self._finalize_tool_call(
            tool_name=tool_name,
            result=result,
            metrics=metrics,
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
        result: str,
        metrics: dict[str, int | str],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        started_at: float,
        defer_telemetry: bool,
    ) -> tuple[str, dict[str, int | str]]:
        if not defer_telemetry:
            await self._record_tool_call_event(
                tool_name=tool_name,
                result=result,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                latency_ms=elapsed_ms(started_at),
            )
        return result, metrics
