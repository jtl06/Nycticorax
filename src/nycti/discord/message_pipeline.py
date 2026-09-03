from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
import logging
import time

import discord

from nycti.bot_support import select_human_mentioned_user_ids
from nycti.diagnostics import is_plsfix_request
from nycti.discord.common import is_configured_guild
from nycti.discord.invocation import InvocationReason
from nycti.discord.progress import DiscordResponseProgress
from nycti.discord.rate_limits import (
    DISCORD_OUTBOUND_CIRCUIT_BREAKER,
    try_discord_request,
)
from nycti.error_debug import (
    send_finalization_failure_debug,
    send_provider_recovery_debug,
    send_reply_generation_error_debug,
)
from nycti.feedback import (
    ResponseDiagnosticSnapshot,
    is_bad_bot_feedback,
    persist_response_diagnostic_snapshot,
)
from nycti.formatting import append_debug_block, format_latency_debug_block
from nycti.message_context import clean_trigger_content
from nycti.progress import ResponseProgressPhase
from nycti.timing import elapsed_ms


LOGGER = logging.getLogger(__name__)
TypingOnce = Callable[[object], Awaitable[None]]
TypingLoop = Callable[..., Awaitable[None]]
EditOrReply = Callable[..., Awaitable[discord.Message | None]]


async def handle_discord_message(
    bot: object,
    message: discord.Message,
    *,
    try_send_typing_once: TypingOnce,
    send_typing_while_pending: TypingLoop,
    edit_progress_or_reply: EditOrReply,
) -> None:
    request_started_at = time.perf_counter()
    if message.author.bot or message.guild is None or getattr(bot, "user", None) is None:
        return
    if not is_configured_guild(
        guild_id=message.guild.id,
        configured_guild_id=bot.settings.discord_guild_id,
    ):
        return
    if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
        LOGGER.debug(
            "Ignoring message %s while the Discord outbound cooldown is active.",
            message.id,
        )
        return
    await bot._remember_observed_members(message)
    invocation_reason = await bot._invocation_policy.reason_for(
        message,
        bot_user=bot.user,
    )
    if invocation_reason is None:
        return
    if (
        invocation_reason is InvocationReason.REPLY
        and is_bad_bot_feedback(message.content)
        and await bot._handle_bad_bot_feedback(message)
    ):
        return

    cleaned_prompt = clean_trigger_content(
        message,
        bot_user_id=bot.user.id,
        invocation_name=bot.settings.discord_invocation_name,
        strip_invocation_name=invocation_reason is InvocationReason.EXPLICIT_NAME,
    )
    effective_prompt = cleaned_prompt or "Reply naturally to the conversation above."
    if is_plsfix_request(effective_prompt):
        await bot._handle_plsfix_request(message, effective_prompt)
        return

    request_key = (message.channel.id, message.author.id)
    if bot._active_requests.has_active(request_key):
        await try_discord_request(
            lambda: message.reply(
                "You already have an active request in this channel. Use `/cancel` to stop it.",
                mention_author=False,
            ),
            circuit_breaker=DISCORD_OUTBOUND_CIRCUIT_BREAKER,
        )
        return

    typing_done = asyncio.Event()
    await try_send_typing_once(message.channel)
    if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
        return
    typing_task = asyncio.create_task(
        send_typing_while_pending(message.channel, typing_done, send_initial=False)
    )
    progress = DiscordResponseProgress(message).start()
    task: asyncio.Task[tuple[str, dict[str, int | str] | None]] | None = None
    try:
        context_started_at = time.perf_counter()
        context_timing_metrics: dict[str, int] = {}
        (
            context_lines,
            context_image_urls,
            image_context_lines,
            context_members,
        ) = await bot._message_context_collector.build_message_context_with_members(
            message,
            timing_metrics=context_timing_metrics,
        )
        member_write_started_at = time.perf_counter()
        await bot._remember_member_objects(
            guild_id=message.guild.id,
            members=context_members,
        )
        context_timing_metrics["ctx_member_write_ms"] = elapsed_ms(member_write_started_at)
        if DISCORD_OUTBOUND_CIRCUIT_BREAKER.is_open:
            return
        context_fetch_ms = elapsed_ms(context_started_at)
        latency_debug_enabled = message.author.id in bot._latency_debug_enabled_users
        memory_debug_enabled = message.author.id in bot._memory_debug_enabled_users
        show_think_enabled = message.author.id in bot._thinking_enabled_users
        task = bot._active_requests.start(
            request_key,
            bot._generate_reply(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                user_global_name=message.author.global_name or message.author.name,
                mentioned_user_ids=select_human_mentioned_user_ids(
                    message.mentions,
                    bot_user_id=bot.user.id,
                ),
                prompt=effective_prompt,
                context_lines=context_lines,
                image_attachment_urls=context_image_urls,
                image_context_lines=image_context_lines,
                source_message_id=message.id,
                request_started_at=request_started_at,
                depth_override=bot._depth_preferences.get(message.author.id),
                collect_latency_debug=True,
                collect_memory_debug=memory_debug_enabled,
                show_think_enabled=show_think_enabled,
                progress=progress,
            ),
        )
        try:
            reply, metrics = await task
        except asyncio.CancelledError:
            progress_message = await progress.claim()
            await edit_progress_or_reply(
                message,
                progress_message,
                "Cancelled your active request.",
                progress=progress,
            )
            return
        except Exception as exc:
            LOGGER.exception(
                "Reply generation failed for message %s in channel %s.",
                message.id,
                message.channel.id,
            )
            await send_reply_generation_error_debug(
                bot,
                channel_id=bot.settings.error_debug_channel_id,
                message=message,
                exc=exc,
            )
            progress_message = await progress.claim()
            with suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                await edit_progress_or_reply(
                    message,
                    progress_message,
                    "I hit an upstream model/provider error for that request. Please retry in a moment.",
                    progress=progress,
                )
            return
        finally:
            bot._active_requests.clear(request_key, task)
        if metrics is not None:
            metrics.update(context_timing_metrics)
        if latency_debug_enabled and metrics is not None:
            metrics["context_fetch_ms"] = context_fetch_ms
            metrics["end_to_end_ms"] = elapsed_ms(request_started_at)
            reply = append_debug_block(reply, format_latency_debug_block(metrics), limit=None)
        reply = bot._render_discord_emojis(reply, message.guild)
        consume_correction = getattr(
            getattr(bot, "_chat_orchestrator", None),
            "consume_memory_correction",
            None,
        )
        correction_context = bool(
            callable(consume_correction) and consume_correction(message.id)
        )
        send_started_at = time.perf_counter()
        await progress.advance(ResponseProgressPhase.DELIVERING)
        progress_message = await progress.claim()
        delivery = await bot._send_message_reply_chunks(
            message,
            reply,
            progress_message=progress_message,
            progress=progress,
        )
        if delivery.complete:
            bot._schedule_memory_extraction(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                source_message_id=message.id,
                current_message=effective_prompt,
                recent_context="\n".join(
                    context_lines[-bot.settings.channel_context_limit :]
                ),
                correction_context=correction_context,
            )
            procedure_learning_queued = bot._schedule_procedure_learning(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                source_message_id=message.id,
                request_text=effective_prompt,
                metrics=metrics,
            )
            if metrics is not None:
                metrics["procedure_learning_queued"] = int(procedure_learning_queued)
        if metrics is not None:
            metrics["reply_send_ms"] = elapsed_ms(send_started_at)
            metrics["context_fetch_ms"] = context_fetch_ms
            metrics["end_to_end_ms"] = elapsed_ms(request_started_at)
            metrics["reply_delivery_complete"] = int(delivery.complete)
            metrics["reply_chunks_delivered"] = len(delivery.messages)
            bot_message_ids = [
                sent.id
                for sent in delivery.messages
                if getattr(sent, "id", None) is not None
            ]
            if delivery.complete and bot_message_ids:
                snapshot = ResponseDiagnosticSnapshot(
                    captured_at=datetime.now(timezone.utc),
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    source_message_id=message.id,
                    source_message_url=message.jump_url,
                    source_user_id=message.author.id,
                    prompt=effective_prompt,
                    context_lines=tuple(context_lines),
                    image_context_lines=tuple(image_context_lines),
                    reply_text=reply,
                    metrics=dict(metrics),
                )
                bot._response_diagnostic_cache.record(
                    snapshot,
                    bot_message_ids=bot_message_ids,
                )
                await persist_response_diagnostic_snapshot(
                    bot.database,
                    snapshot=snapshot,
                    enabled=bool(
                        getattr(bot.settings, "persist_bad_bot_diagnostics", False)
                    ),
                )
            await bot._record_message_debug_stats(
                metrics=metrics,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                source_message_id=message.id,
            )
            await send_provider_recovery_debug(
                bot,
                channel_id=bot.settings.error_debug_channel_id,
                message=message,
                metrics=metrics,
            )
            await send_finalization_failure_debug(
                bot,
                channel_id=bot.settings.error_debug_channel_id,
                message=message,
                metrics=metrics,
            )
    finally:
        try:
            await progress.discard()
        finally:
            typing_done.set()
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task
