from __future__ import annotations

from io import BytesIO
import logging

import discord

LOGGER = logging.getLogger(__name__)
ERROR_DEBUG_MESSAGE_LIMIT = 1900


async def send_error_debug_message(
    bot: discord.Client,
    *,
    channel_id: int | None,
    content: str,
    attachment_text: str | None = None,
    attachment_filename: str = "nycti-debug-request.json",
) -> None:
    if channel_id is None:
        return
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        send = getattr(channel, "send", None)
        if send is None:
            LOGGER.warning("Error debug channel %s does not support send().", channel_id)
            return
        if attachment_text is None:
            await send(content[:2000])
            return
        file = discord.File(
            BytesIO(attachment_text.encode("utf-8")),
            filename=attachment_filename,
        )
        await send(content[:2000], file=file)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        LOGGER.warning("Failed to send error debug message into channel %s.", channel_id, exc_info=True)


async def send_reply_generation_error_debug(
    bot: discord.Client,
    *,
    channel_id: int | None,
    message: discord.Message,
    exc: Exception,
) -> None:
    await send_error_debug_message(
        bot,
        channel_id=channel_id,
        content=format_error_debug_message(
            kind="reply_generation_failed",
            source_channel_id=message.channel.id,
            source_message_id=message.id,
            source_user_id=message.author.id,
            source_message_url=message.jump_url,
            detail=summarize_debug_exception(exc),
        ),
    )


async def send_provider_recovery_debug(
    bot: discord.Client,
    *,
    channel_id: int | None,
    message: discord.Message,
    metrics: dict[str, int | str],
) -> None:
    if "provider_recovery_notice" not in metrics:
        return
    attachment_text = str(metrics.get("provider_recovery_request_json", "") or "").strip() or None
    await send_error_debug_message(
        bot,
        channel_id=channel_id,
        content=format_error_debug_message(
            kind="provider_recovery",
            source_channel_id=message.channel.id,
            source_message_id=message.id,
            source_user_id=message.author.id,
            source_message_url=message.jump_url,
            detail=str(metrics["provider_recovery_notice"]),
            metrics=metrics,
        ),
        attachment_text=attachment_text,
        attachment_filename=f"nycti-provider-request-{message.id}.json",
    )


def format_error_debug_message(
    *,
    kind: str,
    source_channel_id: int,
    source_message_id: int,
    source_user_id: int,
    source_message_url: str,
    detail: str,
    metrics: dict[str, int | str] | None = None,
) -> str:
    lines = [
        "nycti_error_debug",
        f"type: {_safe_debug_value(kind)}",
        f"source_channel_id: {source_channel_id}",
        f"source_message_id: {source_message_id}",
        f"source_user_id: {source_user_id}",
        f"source_message_url: {_safe_debug_value(source_message_url)}",
        f"detail: {_safe_debug_value(detail, limit=700)}",
    ]
    if metrics is not None:
        for key in (
            "active_chat_model",
            "chat_model",
            "tool_planner_model",
            "tool_planner_need_tools",
            "tool_planner_tools",
            "exposed_tools",
            "native_tool_fallback_count",
            "provider_recovery_detail",
            "tool_call_count",
            "web_search_requested",
            "chat_empty_turn_feature",
        ):
            if key in metrics:
                lines.append(f"{key}: {_safe_debug_value(str(metrics[key]))}")
    body = "\n".join(lines)
    if len(body) > ERROR_DEBUG_MESSAGE_LIMIT:
        body = body[: ERROR_DEBUG_MESSAGE_LIMIT - 3].rstrip() + "..."
    return "```text\n" + body + "\n```"


def summarize_debug_exception(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if not text:
        text = "(no exception message)"
    return f"{type(exc).__name__}: {text}"


def _safe_debug_value(value: str, *, limit: int = 300) -> str:
    cleaned = " ".join(value.replace("```", "'''").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."
