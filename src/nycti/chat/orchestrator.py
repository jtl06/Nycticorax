from __future__ import annotations

import asyncio
import logging
import time

import discord

from nycti.browser import BrowserClient
from nycti.channel_aliases import ChannelAliasService
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import WEB_SEARCH_TOOL_NAME, build_chat_tools
from nycti.config import Settings
from nycti.db.session import Database
from nycti.formatting import extract_think_content
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
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
        browser_client: BrowserClient | None = None,
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
            tavily_client=tavily_client,
            browser_client=browser_client,
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
        required_tools: set[str] = set()
        if search_requested:
            required_tools.add(WEB_SEARCH_TOOL_NAME)
        used_tools: set[str] = set()
        latest_tool_results: list[str] = []
        reasoning_parts: list[str] = []
        if metrics is not None:
            metrics["tool_call_count"] = 0
        for _ in range(MAX_CHAT_TOOL_ROUNDS):
            chat_started_at = time.perf_counter()
            turn = await self.llm_client.complete_chat_turn(
                model=chat_model,
                feature="chat_reply",
                max_tokens=self.settings.max_completion_tokens,
                temperature=0.7,
                messages=messages,
                tools=tools,
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
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Before answering, you still must call these tools at least once: "
                                + ", ".join(sorted(missing_required_tools))
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
                    rewritten_text, rewrite_reasoning = await self._maybe_rewrite_tool_answer(
                        answer_text=turn.text,
                        used_tools=used_tools,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        metrics=metrics,
                    )
                    reasoning_parts.extend(rewrite_reasoning)
                    return rewritten_text, reasoning_parts
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
        )
        reasoning_parts.extend(final_reasoning)
        return text, reasoning_parts

    async def _maybe_rewrite_tool_answer(
        self,
        *,
        answer_text: str,
        used_tools: set[str],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
    ) -> tuple[str, list[str]]:
        if not self._should_run_tool_answer_rewrite(answer_text=answer_text, used_tools=used_tools):
            return answer_text, []
        rewrite_started_at = time.perf_counter()
        try:
            rewrite_result = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_memory_model,
                feature="chat_reply_rewrite",
                max_tokens=min(self.settings.max_completion_tokens, 220),
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Rewrite the assistant draft into a concise final Discord reply. "
                            "Keep all concrete facts, numbers, and uncertainty from the draft. "
                            "Do not include markdown tables or raw tool dumps. "
                            "Do not mention tool names."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Draft answer:\n"
                            f"{answer_text}\n\n"
                            "Return only the rewritten final answer. Keep it short and to the point."
                        ),
                    },
                ],
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Tool-answer rewrite failed; returning original answer.")
            return answer_text, []

        rewrite_reasoning = _collect_reasoning(rewrite_result)
        if metrics is not None:
            metrics["chat_rewrite_count"] = int(metrics.get("chat_rewrite_count", 0)) + 1
            metrics["chat_rewrite_ms"] = int(metrics.get("chat_rewrite_ms", 0)) + _elapsed_ms(rewrite_started_at)
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(rewrite_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + rewrite_result.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + rewrite_result.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + rewrite_result.usage.total_tokens
            metrics["chat_rewrite_model"] = rewrite_result.usage.model
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
        turn = await self.llm_client.complete_chat_turn(
            model=chat_model,
            feature="chat_reply_final",
            max_tokens=self.settings.max_completion_tokens,
            temperature=0.4,
            messages=final_messages,
            tools=None,
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
            return turn.text, reasoning_parts
        if latest_tool_results:
            return fallback_tool_result(latest_tool_results[-1]), reasoning_parts
        return "I hit the tool-call limit for this reply. Try asking in a more focused way.", reasoning_parts

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


def _collect_reasoning(turn) -> list[str]:
    parts: list[str] = []
    if turn.reasoning_content:
        parts.append(turn.reasoning_content)
    inline_think = extract_think_content(turn.raw_text)
    parts.extend(inline_think)
    return parts


def _append_raw_tool_trace(metrics: dict[str, int | str], raw_text: str) -> None:
    cleaned = raw_text.strip()
    if not cleaned or "<|tool_call" not in cleaned:
        return
    existing = str(metrics.get("raw_tool_trace", "")).strip()
    if existing:
        metrics["raw_tool_trace"] = existing + "\n\n---\n\n" + cleaned
        return
    metrics["raw_tool_trace"] = cleaned


def _looks_like_raw_tavily_dump(text: str) -> bool:
    normalized = text.strip()
    return normalized.startswith(
        (
            "Tavily web results for:",
            "Tavily extract for:",
            "Tavily image results for:",
        )
    )


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
