from __future__ import annotations

import asyncio
import logging
import time

import discord

from nycti.agent_trace import AgentTrace
from nycti.browser import BrowserClient
from nycti.channel_aliases import ChannelAliasService
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import WEB_SEARCH_TOOL_NAME, build_chat_tools
from nycti.chat.orchestrator_support import (
    EVIDENCE_TOOL_NAMES,
    MAX_LENGTH_CONTINUATION_ROUNDS,
    append_raw_tool_trace as _append_raw_tool_trace,
    chat_reply_max_tokens as _chat_reply_max_tokens,
    collect_reasoning as _collect_reasoning,
    elapsed_ms as _elapsed_ms,
    first_latency_metric as _first_latency_metric,
    first_result_line as _first_result_line,
    format_available_tool_guidance as _format_available_tool_guidance,
    format_inline_tool_fallback_guidance as _format_inline_tool_fallback_guidance,
    format_tool_evidence as _format_tool_evidence,
    join_continuation_parts as _join_continuation_parts,
    looks_like_raw_tavily_dump as _looks_like_raw_tavily_dump,
    should_continue_answer as _should_continue_answer,
    tool_call_signature as _tool_call_signature,
    tool_names as _tool_names,
    tool_synthesis_max_tokens as _tool_synthesis_max_tokens,
    write_agent_trace as _write_agent_trace,
)
from nycti.config import Settings
from nycti.db.session import Database
from nycti.llm.client import LLMChatTurn, OpenAIClient
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.yahoo import YahooFinanceClient
from nycti.youtube import YouTubeTranscriptClient
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
MAX_CHAT_TOOL_ROUNDS = 6


class ChatOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
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
        self.settings = settings
        self.database = database
        self.llm_client = llm_client
        self.tavily_client = tavily_client
        self.memory_service = memory_service
        self.channel_alias_service = channel_alias_service
        self.reminder_service = reminder_service
        self.tool_executor = ChatToolExecutor(
            database=database,
            settings=settings,
            llm_client=llm_client,
            market_data_client=market_data_client,
            yahoo_finance_client=yahoo_finance_client,
            tavily_client=tavily_client,
            browser_client=browser_client,
            youtube_client=youtube_client,
            memory_service=memory_service,
            channel_alias_service=channel_alias_service,
            reminder_service=reminder_service,
            bot=bot,
        )

    async def run_chat_with_tools(
        self,
        *,
        chat_model: str,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        search_requested: bool,
        metrics: dict[str, int | str] | None,
    ) -> tuple[str, list[str]]:
        tools = build_chat_tools()
        required_tools = {WEB_SEARCH_TOOL_NAME} if search_requested else set()
        trace = AgentTrace(enabled=metrics is not None)
        available_tool_names = _tool_names(tools)
        native_tools_enabled = True
        used_tools: set[str] = set()
        seen_tool_call_signatures: set[str] = set()
        latest_tool_results: list[str] = []
        reasoning_parts: list[str] = []
        if metrics is not None:
            metrics["tool_call_count"] = 0
            metrics["exposed_tool_count"] = len(available_tool_names)
            metrics["exposed_tools"] = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
        if tools:
            messages.append(
                {
                    "role": "user",
                    "content": _format_available_tool_guidance(
                        available_tool_names=available_tool_names,
                    ),
                }
            )
        if tools:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool-loop discipline: after tools return enough evidence, stop calling tools and answer. "
                        "Do not repeat the same tool call with the same arguments unless the prior result was unusable."
                    ),
                }
            )
        reply_max_tokens = _chat_reply_max_tokens(self.settings)
        for _ in range(MAX_CHAT_TOOL_ROUNDS):
            chat_started_at = time.perf_counter()
            turn = await self.llm_client.complete_chat_turn(
                model=chat_model,
                feature="chat_reply",
                max_tokens=reply_max_tokens,
                temperature=0.7,
                messages=messages,
                tools=tools,
                use_native_tools=native_tools_enabled,
            )
            if turn.native_tool_calling_failed and native_tools_enabled:
                native_tools_enabled = False
                if metrics is not None:
                    metrics["native_tool_fallback_count"] = int(metrics.get("native_tool_fallback_count", 0)) + 1
                    metrics["provider_recovery_notice"] = (
                        "native tool request was rejected; switched to plain/XML tool fallback"
                    )
                    if turn.native_tool_failure_request_json:
                        metrics["provider_recovery_request_json"] = turn.native_tool_failure_request_json
                LOGGER.warning(
                    "Disabling native tool schemas for remaining chat loop after provider rejected them. model=%s tools=%s.",
                    turn.usage.model,
                    ",".join(sorted(available_tool_names)) or "(none)",
                )
            trace.mark(
                "chat_turn",
                started_at=chat_started_at,
                attrs={
                    "model": turn.usage.model,
                    "feature": turn.usage.feature,
                    "tokens": turn.usage.total_tokens,
                    "tool_calls": len(turn.tool_calls),
                },
            )
            if metrics is not None:
                metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(chat_started_at)
                metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
                metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
                metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
                metrics["active_chat_model"] = turn.usage.model
                _append_raw_tool_trace(metrics, turn.raw_text)
            reasoning_parts.extend(_collect_reasoning(turn))
            if metrics is not None:
                usage_write_ms, commit_ms = await self._record_usage(
                    usage=turn.usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + usage_write_ms
                metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
            else:
                await self._record_usage(
                    usage=turn.usage,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
            if not turn.tool_calls:
                missing_required_tools = required_tools - used_tools
                if missing_required_tools:
                    LOGGER.warning(
                        "Chat turn returned no text/tool calls before required tools were used; missing=%s model=%s.",
                        ",".join(sorted(missing_required_tools)),
                        turn.usage.model,
                    )
                    if metrics is not None:
                        metrics["chat_empty_turn_count"] = int(metrics.get("chat_empty_turn_count", 0)) + 1
                        metrics["chat_empty_turn_feature"] = "chat_reply_missing_required_tool"
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                _format_inline_tool_fallback_guidance(
                                    available_tool_names=available_tool_names,
                                    required_tool_names=missing_required_tools,
                                )
                                if not native_tools_enabled and tools
                                else (
                                    "Before answering, you still must call these tools at least once: "
                                    + ", ".join(sorted(missing_required_tools))
                                )
                            ),
                        }
                    )
                    continue
                if turn.text:
                    if _looks_like_raw_tavily_dump(turn.text):
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Do not paste raw Tavily tool output. "
                                    "Rewrite a concise direct answer in your own words using the tool sources."
                                ),
                            }
                        )
                        continue
                    answer_text, continuation_reasoning = await self._maybe_continue_length_limited_answer(
                        chat_model=chat_model,
                        messages=messages,
                        initial_turn=turn,
                        max_tokens=reply_max_tokens,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        metrics=metrics,
                        trace=trace,
                    )
                    reasoning_parts.extend(continuation_reasoning)
                    rewritten_text, rewrite_reasoning = await self._maybe_synthesize_tool_answer(
                        answer_text=answer_text,
                        used_tools=used_tools,
                        latest_tool_results=latest_tool_results,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        metrics=metrics,
                        trace=trace,
                    )
                    reasoning_parts.extend(rewrite_reasoning)
                    _write_agent_trace(metrics, trace)
                    return rewritten_text, reasoning_parts
                LOGGER.warning(
                    "Chat turn returned no text and no tool calls; forcing final answer. model=%s used_tools=%s latest_tool_results=%s",
                    turn.usage.model,
                    ",".join(sorted(used_tools)) or "(none)",
                    len(latest_tool_results),
                )
                if metrics is not None:
                    metrics["chat_empty_turn_count"] = int(metrics.get("chat_empty_turn_count", 0)) + 1
                    metrics["chat_empty_turn_feature"] = "chat_reply"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        }
                        for tool_call in turn.tool_calls
                    ],
                }
            )
            current_signatures = {
                _tool_call_signature(tool_call.name, tool_call.arguments)
                for tool_call in turn.tool_calls
            }
            if current_signatures and current_signatures <= seen_tool_call_signatures:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You already made those exact tool calls. Stop using tools now and answer from "
                            "the existing tool results and context."
                        ),
                    }
                )
                break
            seen_tool_call_signatures.update(current_signatures)
            used_tools.update(tool_call.name for tool_call in turn.tool_calls)
            if metrics is not None:
                metrics["tool_call_count"] = int(metrics.get("tool_call_count", 0)) + len(turn.tool_calls)
            tool_results = await asyncio.gather(
                *[
                    self._execute_chat_tool_call(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        source_message_id=source_message_id,
                    )
                    for tool_call in turn.tool_calls
                ]
            )
            for tool_call, (tool_result, tool_metrics) in zip(turn.tool_calls, tool_results):
                trace.add(
                    f"tool:{tool_call.name}",
                    elapsed_ms=_first_latency_metric(tool_metrics),
                    attrs={
                        "result": _first_result_line(tool_result),
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )
                latest_tool_results.append(tool_result)
                if metrics is not None:
                    for key, value in tool_metrics.items():
                        if isinstance(value, int):
                            metrics[key] = int(metrics.get(key, 0)) + value
                        else:
                            metrics[key] = value

        text, final_reasoning = await self._force_final_answer(
            chat_model=chat_model,
            messages=messages,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            metrics=metrics,
            latest_tool_results=latest_tool_results,
            used_tools=used_tools,
            trace=trace,
        )
        reasoning_parts.extend(final_reasoning)
        _write_agent_trace(metrics, trace)
        return text, reasoning_parts

    async def _maybe_synthesize_tool_answer(
        self,
        *,
        answer_text: str,
        used_tools: set[str],
        latest_tool_results: list[str],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        trace: AgentTrace,
    ) -> tuple[str, list[str]]:
        if not self._should_run_tool_answer_rewrite(answer_text=answer_text, used_tools=used_tools):
            return answer_text, []
        rewrite_started_at = time.perf_counter()
        synthesis_max_tokens = _tool_synthesis_max_tokens(self.settings)
        synthesis_messages = [
            {
                "role": "system",
                "content": (
                    "Synthesize a concise final Discord reply from the tool evidence and draft. "
                    "Internally check: facts found, uncertain parts, final answer. "
                    "Output only the final answer. Keep concrete facts, numbers, links, and uncertainty. "
                    "Do not include markdown tables or raw tool dumps. "
                    "Do not mention tool names."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Tools used: "
                    + ", ".join(sorted(used_tools))
                    + "\n\nTool evidence:\n"
                    + _format_tool_evidence(latest_tool_results)
                    + "\n\n"
                    "Draft answer:\n"
                    f"{answer_text}\n\n"
                    "Return only the final answer. Keep it short and to the point."
                ),
            },
        ]
        try:
            rewrite_result = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_memory_model,
                feature="chat_reply_synthesis",
                max_tokens=synthesis_max_tokens,
                temperature=0.2,
                messages=synthesis_messages,
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Tool-answer synthesis failed; returning original answer.")
            return answer_text, []

        rewrite_reasoning = _collect_reasoning(rewrite_result)
        trace.mark(
            "chat_synthesis",
            started_at=rewrite_started_at,
            attrs={
                "model": rewrite_result.usage.model,
                "tokens": rewrite_result.usage.total_tokens,
                "tools": ",".join(sorted(used_tools)),
            },
        )
        if metrics is not None:
            metrics["chat_rewrite_count"] = int(metrics.get("chat_rewrite_count", 0)) + 1
            metrics["chat_rewrite_ms"] = int(metrics.get("chat_rewrite_ms", 0)) + _elapsed_ms(rewrite_started_at)
            metrics["chat_synthesis_count"] = int(metrics.get("chat_synthesis_count", 0)) + 1
            metrics["chat_synthesis_ms"] = int(metrics.get("chat_synthesis_ms", 0)) + _elapsed_ms(rewrite_started_at)
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(rewrite_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + rewrite_result.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + rewrite_result.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + rewrite_result.usage.total_tokens
            metrics["chat_rewrite_model"] = rewrite_result.usage.model
            metrics["chat_synthesis_model"] = rewrite_result.usage.model
            _append_raw_tool_trace(metrics, rewrite_result.raw_text)

        if metrics is not None:
            usage_write_ms, commit_ms = await self._record_usage(
                usage=rewrite_result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + usage_write_ms
            metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
        else:
            await self._record_usage(
                usage=rewrite_result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )

        rewritten = rewrite_result.text.strip()
        if not rewritten:
            return answer_text, rewrite_reasoning
        if _looks_like_raw_tavily_dump(rewritten):
            return fallback_tool_result(rewritten), rewrite_reasoning
        rewritten, continuation_reasoning = await self._maybe_continue_length_limited_answer(
            chat_model=self.settings.openai_memory_model,
            messages=synthesis_messages,
            initial_turn=rewrite_result,
            max_tokens=synthesis_max_tokens,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            metrics=metrics,
            trace=trace,
        )
        rewrite_reasoning.extend(continuation_reasoning)
        return rewritten, rewrite_reasoning

    def _should_run_tool_answer_rewrite(
        self,
        *,
        answer_text: str,
        used_tools: set[str],
    ) -> bool:
        if not self.settings.tool_answer_rewrite_enabled:
            return False
        if not used_tools:
            return False
        normalized = answer_text.strip()
        if not normalized:
            return False
        if used_tools & EVIDENCE_TOOL_NAMES:
            return True
        if _looks_like_raw_tavily_dump(normalized):
            return True
        if len(normalized) >= self.settings.tool_answer_rewrite_min_chars:
            return True
        return False

    async def _force_final_answer(
        self,
        *,
        chat_model: str,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        latest_tool_results: list[str],
        used_tools: set[str],
        trace: AgentTrace,
    ) -> tuple[str, list[str]]:
        final_messages = list(messages)
        final_messages.append(
            {
                "role": "user",
                "content": (
                    "Stop using tools now. Give the final answer directly from the tool results and context you already have. "
                    "Do not copy or paste raw tool output blocks; synthesize in your own words."
                ),
            }
        )
        chat_started_at = time.perf_counter()
        reply_max_tokens = _chat_reply_max_tokens(self.settings)
        turn = await self.llm_client.complete_chat_turn(
            model=chat_model,
            feature="chat_reply_final",
            max_tokens=reply_max_tokens,
            temperature=0.4,
            messages=final_messages,
            tools=None,
        )
        trace.mark(
            "chat_final",
            started_at=chat_started_at,
            attrs={
                "model": turn.usage.model,
                "tokens": turn.usage.total_tokens,
            },
        )
        if metrics is not None:
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(chat_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
            metrics["active_chat_model"] = turn.usage.model
            _append_raw_tool_trace(metrics, turn.raw_text)
        reasoning_parts = _collect_reasoning(turn)
        if metrics is not None:
            usage_write_ms, commit_ms = await self._record_usage(
                usage=turn.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + usage_write_ms
            metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
        else:
            await self._record_usage(
                usage=turn.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
        if turn.text:
            if _looks_like_raw_tavily_dump(turn.text):
                return fallback_tool_result(turn.text), reasoning_parts
            answer_text, continuation_reasoning = await self._maybe_continue_length_limited_answer(
                chat_model=chat_model,
                messages=final_messages,
                initial_turn=turn,
                max_tokens=reply_max_tokens,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                metrics=metrics,
                trace=trace,
            )
            reasoning_parts.extend(continuation_reasoning)
            return answer_text, reasoning_parts
        LOGGER.warning(
            "Forced final chat turn returned empty text. model=%s used_tools=%s latest_tool_results=%s prompt_tokens=%s completion_tokens=%s",
            turn.usage.model,
            ",".join(sorted(used_tools)) or "(none)",
            len(latest_tool_results),
            turn.usage.prompt_tokens,
            turn.usage.completion_tokens,
        )
        if metrics is not None:
            metrics["chat_empty_final_count"] = int(metrics.get("chat_empty_final_count", 0)) + 1
        if latest_tool_results:
            synthesized_text, synthesis_reasoning = await self._maybe_synthesize_tool_answer(
                answer_text=(
                    "The main chat loop reached its tool-call stop condition. "
                    "Give the best concise answer possible from the existing tool evidence."
                ),
                used_tools=used_tools,
                latest_tool_results=latest_tool_results,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                metrics=metrics,
                trace=trace,
            )
            reasoning_parts.extend(synthesis_reasoning)
            if synthesized_text:
                return synthesized_text, reasoning_parts
            return fallback_tool_result(latest_tool_results[-1]), reasoning_parts
        return "I couldn't generate a clean reply from that request. Try rephrasing it a bit.", reasoning_parts

    async def _execute_chat_tool_call(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
    ) -> tuple[str, dict[str, int | str]]:
        return await self.tool_executor.execute(
            tool_name=tool_name,
            arguments=arguments,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_message_id=source_message_id,
        )

    async def _maybe_continue_length_limited_answer(
        self,
        *,
        chat_model: str,
        messages: list[dict[str, object]],
        initial_turn: LLMChatTurn,
        max_tokens: int,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        trace: AgentTrace,
    ) -> tuple[str, list[str]]:
        if not _should_continue_answer(initial_turn, max_tokens=max_tokens):
            return initial_turn.text, []

        if metrics is not None:
            metrics["chat_length_finish_count"] = int(metrics.get("chat_length_finish_count", 0)) + 1

        parts = [initial_turn.text]
        reasoning_parts: list[str] = []
        continuation_messages = list(messages)
        continuation_messages.append({"role": "assistant", "content": initial_turn.text})

        for _ in range(MAX_LENGTH_CONTINUATION_ROUNDS):
            continuation_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous answer was cut off by the token limit. Continue exactly where it stopped. "
                        "Do not restart, summarize, or repeat earlier rows."
                    ),
                }
            )
            chat_started_at = time.perf_counter()
            turn = await self.llm_client.complete_chat_turn(
                model=chat_model,
                feature="chat_reply_continuation",
                max_tokens=max_tokens,
                temperature=0.4,
                messages=continuation_messages,
                tools=None,
            )
            trace.mark(
                "chat_continuation",
                started_at=chat_started_at,
                attrs={
                    "model": turn.usage.model,
                    "tokens": turn.usage.total_tokens,
                    "finish_reason": turn.finish_reason or "(none)",
                },
            )
            await self._record_chat_turn_metrics_and_usage(
                turn=turn,
                started_at=chat_started_at,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                metrics=metrics,
            )
            reasoning_parts.extend(_collect_reasoning(turn))
            if metrics is not None:
                metrics["chat_continuation_count"] = int(metrics.get("chat_continuation_count", 0)) + 1
            if not turn.text:
                break
            parts.append(turn.text)
            continuation_messages.append({"role": "assistant", "content": turn.text})
            if not _should_continue_answer(turn, max_tokens=max_tokens):
                break

        return _join_continuation_parts(parts), reasoning_parts

    async def _record_chat_turn_metrics_and_usage(
        self,
        *,
        turn: LLMChatTurn,
        started_at: float,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
    ) -> None:
        if metrics is not None:
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + turn.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + turn.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + turn.usage.total_tokens
            metrics["active_chat_model"] = turn.usage.model
            _append_raw_tool_trace(metrics, turn.raw_text)
            usage_write_ms, commit_ms = await self._record_usage(
                usage=turn.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + usage_write_ms
            metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
            return

        await self._record_usage(
            usage=turn.usage,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )

    async def _record_usage(
        self,
        *,
        usage,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> tuple[int, int]:
        usage_write_started_at = time.perf_counter()
        async with self.database.session() as session:
            await record_usage(
                session,
                usage=usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            usage_write_ms = _elapsed_ms(usage_write_started_at)
            commit_started_at = time.perf_counter()
            await session.commit()
        return usage_write_ms, _elapsed_ms(commit_started_at)
