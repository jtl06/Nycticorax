from __future__ import annotations

import logging
import time

LOGGER = logging.getLogger(__name__)


class ToolTelemetryMixin:
    async def _record_tool_call_event(
        self,
        *,
        tool_name: str,
        result: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        latency_ms: int,
    ) -> None:
        if not hasattr(self.database, "session"):
            return
        status = self._tool_call_status(result)
        try:
            async with self.database.session() as session:
                from nycti.usage import record_tool_call

                await record_tool_call(
                    session,
                    tool_name=tool_name,
                    status=status,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    latency_ms=latency_ms,
                )
                await session.commit()
        except Exception:  # pragma: no cover - defensive telemetry path
            LOGGER.exception("Tool call event logging failed for tool %s.", tool_name)

    @staticmethod
    def _tool_call_status(result: str) -> str:
        normalized = result.strip().casefold()
        if not normalized:
            return "ok"
        if "no older messages beyond the default recent window" in normalized:
            return "empty"
        failure_markers = (
            " failed",
            "unknown tool",
            "not configured",
            "malformed",
            "missing",
            "invalid",
            "unavailable",
            "could not",
        )
        if any(marker in normalized for marker in failure_markers):
            return "error"
        return "ok"

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


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
