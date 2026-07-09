from __future__ import annotations

import logging

from nycti.chat.run_state import ToolStatus

LOGGER = logging.getLogger(__name__)


class ToolTelemetryMixin:
    async def _record_tool_call_event(
        self,
        *,
        tool_name: str,
        status: ToolStatus,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        latency_ms: int,
    ) -> None:
        if not hasattr(self.database, "session"):
            return
        try:
            async with self.database.session() as session:
                from nycti.usage import record_tool_call

                await record_tool_call(
                    session,
                    tool_name=tool_name,
                    status=str(status),
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    latency_ms=latency_ms,
                )
                await session.commit()
        except Exception:  # pragma: no cover - defensive telemetry path
            LOGGER.exception("Tool call event logging failed for tool %s.", tool_name)

    async def _record_auxiliary_llm_usage(
        self,
        *,
        usage,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
    ) -> None:
        if not hasattr(self.database, "session"):
            return
        try:
            async with self.database.session() as session:
                from nycti.usage import record_usage

                await record_usage(
                    session,
                    usage=usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                await session.commit()
        except Exception:  # pragma: no cover - defensive telemetry path
            LOGGER.exception("Auxiliary LLM usage logging failed.")
