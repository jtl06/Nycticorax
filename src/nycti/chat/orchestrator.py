from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time

import discord

from nycti.agent_trace import AgentTrace
from nycti.browser import BrowserClient
from nycti.channel_aliases import ChannelAliasService
from nycti.chat.tool_fallback import fallback_tool_result
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.registry import build_tool_planner_catalog
from nycti.chat.tools.schemas import (
    BROWSER_EXTRACT_TOOL_NAME,
    CREATE_REMINDER_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
    build_chat_tools,
)
from nycti.config import Settings
from nycti.db.session import Database
from nycti.formatting import extract_think_content
from nycti.llm.client import OpenAIClient
from nycti.memory.service import MemoryService
from nycti.reminders.service import ReminderService
from nycti.tavily.client import TavilyClient
from nycti.twelvedata.client import TwelveDataClient
from nycti.youtube import YouTubeTranscriptClient
from nycti.usage import record_usage

LOGGER = logging.getLogger(__name__)
MAX_CHAT_TOOL_ROUNDS = 6
TOOL_PLANNER_CONTEXT_CHAR_LIMIT = 2800
TOOL_SYNTHESIS_EVIDENCE_CHAR_LIMIT = 9000
EVIDENCE_TOOL_NAMES = {
    BROWSER_EXTRACT_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
}
ACTION_TOOL_NAMES = {
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
    UPDATE_PERSONAL_PROFILE_TOOL_NAME,
}


@dataclass(frozen=True, slots=True)
class ChatToolPlan:
    need_tools: bool
    tools_to_try: tuple[str, ...]
    freshness_required: bool
    risk_level: str
    reason: str


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
        all_tools = build_chat_tools()
        all_available_tool_names = _tool_names(all_tools)
        required_tools = {WEB_SEARCH_TOOL_NAME} if search_requested else set()
        trace = AgentTrace(enabled=metrics is not None)
        plan = await self._maybe_plan_tool_use(
            messages=messages,
            available_tool_names=all_available_tool_names,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            metrics=metrics,
            trace=trace,
        )
        tools = all_tools
        available_tool_names = _tool_names(tools)
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
                        plan=plan,
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
                    rewritten_text, rewrite_reasoning = await self._maybe_synthesize_tool_answer(
                        answer_text=turn.text,
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

    async def _maybe_plan_tool_use(
        self,
        *,
        messages: list[dict[str, object]],
        available_tool_names: set[str],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        metrics: dict[str, int | str] | None,
        trace: AgentTrace,
    ) -> ChatToolPlan | None:
        if not self.settings.tool_planner_enabled:
            return None
        planner_started_at = time.perf_counter()
        try:
            planner_result = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_memory_model,
                feature="chat_tool_plan",
                max_tokens=180,
                temperature=0.0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Choose which tools are likely useful before the main chat model answers. "
                            "Return compact JSON only with keys: need_tools boolean, tools_to_try array, "
                            "freshness_required boolean, risk_level low|medium|high, reason string. "
                            "`tools_to_try` is a preferred execution order or subset when tool use is needed. "
                            "Prefer tools for current facts, market data, exact URLs, images, reminders, "
                            "cross-channel sends, math/data transforms, or older Discord context. "
                            "Use no tools for opinions or ordinary chat."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Available tool catalog:\n"
                            + build_tool_planner_catalog(available_tool_names)
                            + "\n\nCurrent request/context excerpt:\n"
                            + _latest_message_excerpt(messages, TOOL_PLANNER_CONTEXT_CHAR_LIMIT)
                        ),
                    },
                ],
            )
        except Exception:  # pragma: no cover - defensive provider fallback
            LOGGER.exception("Tool-planning prepass failed; continuing without it.")
            return None
        trace.mark(
            "tool_plan",
            started_at=planner_started_at,
            attrs={
                "model": planner_result.usage.model,
                "tokens": planner_result.usage.total_tokens,
            },
        )

        if metrics is not None:
            metrics["tool_planner_count"] = int(metrics.get("tool_planner_count", 0)) + 1
            metrics["tool_planner_ms"] = int(metrics.get("tool_planner_ms", 0)) + _elapsed_ms(planner_started_at)
            metrics["chat_llm_ms"] = int(metrics.get("chat_llm_ms", 0)) + _elapsed_ms(planner_started_at)
            metrics["chat_prompt_tokens"] = int(metrics.get("chat_prompt_tokens", 0)) + planner_result.usage.prompt_tokens
            metrics["chat_completion_tokens"] = int(metrics.get("chat_completion_tokens", 0)) + planner_result.usage.completion_tokens
            metrics["chat_total_tokens"] = int(metrics.get("chat_total_tokens", 0)) + planner_result.usage.total_tokens
            metrics["tool_planner_model"] = planner_result.usage.model
            _append_raw_tool_trace(metrics, planner_result.raw_text)

        if metrics is not None:
            usage_write_ms, commit_ms = await self._record_usage(
                usage=planner_result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            metrics["chat_usage_write_ms"] = int(metrics.get("chat_usage_write_ms", 0)) + usage_write_ms
            metrics["chat_commit_ms"] = int(metrics.get("chat_commit_ms", 0)) + commit_ms
        else:
            await self._record_usage(
                usage=planner_result.usage,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )

        plan = _parse_tool_plan(planner_result.text, available_tool_names)
        if plan is None:
            return None
        trace.mark(
            "tool_plan_parse",
            started_at=time.perf_counter(),
            attrs={
                "need_tools": plan.need_tools,
                "tools": ",".join(plan.tools_to_try),
                "risk": plan.risk_level,
            },
        )
        if metrics is not None:
            metrics["tool_planner_need_tools"] = str(plan.need_tools).lower()
            if plan.tools_to_try:
                metrics["tool_planner_tools"] = ", ".join(plan.tools_to_try)
            metrics["tool_planner_risk"] = plan.risk_level
        return plan

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
        try:
            rewrite_result = await self.llm_client.complete_chat_turn(
                model=self.settings.openai_memory_model,
                feature="chat_reply_synthesis",
                max_tokens=min(self.settings.max_completion_tokens, 220),
                temperature=0.2,
                messages=[
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
                ],
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
        turn = await self.llm_client.complete_chat_turn(
            model=chat_model,
            feature="chat_reply_final",
            max_tokens=self.settings.max_completion_tokens,
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
            return turn.text, reasoning_parts
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


def _tool_names(tools: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _latest_message_excerpt(messages: list[dict[str, object]], char_limit: int) -> str:
    if not messages:
        return ""
    content = messages[-1].get("content", "")
    if isinstance(content, str):
        text = content
    else:
        text = str(content)
    return _truncate_text(text.strip(), char_limit)


def _parse_tool_plan(text: str, available_tool_names: set[str]) -> ChatToolPlan | None:
    data = _load_json_object(text)
    if data is None:
        return None
    tools = _parse_tool_name_list(data.get("tools_to_try"), available_tool_names)
    need_tools = bool(data.get("need_tools"))
    if not need_tools:
        tools = []
    risk_level = str(data.get("risk_level", "low")).strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"
    reason = str(data.get("reason", "")).strip()
    return ChatToolPlan(
        need_tools=need_tools,
        tools_to_try=tuple(tools),
        freshness_required=bool(data.get("freshness_required")),
        risk_level=risk_level,
        reason=_truncate_text(reason, 180),
    )


def _parse_tool_name_list(value: object, available_tool_names: set[str]) -> list[str]:
    tools: list[str] = []
    if not isinstance(value, list):
        return tools
    for item in value:
        if isinstance(item, str) and item in available_tool_names and item not in tools:
            tools.append(item)
    return tools


def _load_json_object(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    first_brace = stripped.find("{")
    if first_brace >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(stripped[first_brace:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _format_tool_plan_guidance(plan: ChatToolPlan) -> str:
    if plan.need_tools:
        tools = ", ".join(plan.tools_to_try) if plan.tools_to_try else "(model should choose from available tools)"
        guidance = (
            "Tool planning guidance:\n"
            f"- need_tools: true\n"
            f"- tools_to_try: {tools}\n"
            f"- freshness_required: {str(plan.freshness_required).lower()}\n"
            f"- risk_level: {plan.risk_level}\n"
        )
        if plan.reason:
            guidance += f"- reason: {plan.reason}\n"
        guidance += "Use this plan unless the current request or context clearly requires a different tool path."
        return guidance
    guidance = (
        "Tool planning guidance:\n"
        "- need_tools: false\n"
        f"- risk_level: {plan.risk_level}\n"
    )
    if plan.reason:
        guidance += f"- reason: {plan.reason}\n"
    guidance += "Answer without tools unless the request clearly depends on fresh facts, exact external content, or an action."
    return guidance


def _format_available_tool_guidance(
    *,
    available_tool_names: set[str],
    plan: ChatToolPlan | None,
) -> str:
    names = ", ".join(sorted(available_tool_names)) if available_tool_names else "(none)"
    guidance = (
        "Available tools this turn:\n"
        f"- {names}\n"
        "Use only these native tools if a tool is needed. Do not write textual or XML tool-call markup in the reply."
    )
    guidance += (
        "\nFor market questions that compare live data against any historical benchmark, record, prior close, "
        "or dated reference point, use tools to verify both sides of the comparison. Do not answer historical "
        "market records from model memory."
    )
    guidance += (
        "\nThe current local date/time is provided in the request context and is authoritative. "
        "When a factual answer depends on events, prices, records, releases, or historical facts that may be newer "
        "than model training or could have changed, use search or a domain tool to ground it."
    )
    action_tools = sorted(available_tool_names & ACTION_TOOL_NAMES)
    if action_tools:
        guidance += (
            "\nAction tools exposed this turn: "
            + ", ".join(action_tools)
            + ". Call them only when the user clearly requested that action."
        )
    if plan is not None and plan.need_tools:
        guidance += "\n\n" + _format_tool_plan_guidance(plan)
    return guidance


def _format_tool_evidence(tool_results: list[str]) -> str:
    if not tool_results:
        return "(no tool evidence captured)"
    blocks: list[str] = []
    remaining = TOOL_SYNTHESIS_EVIDENCE_CHAR_LIMIT
    for index, result in enumerate(tool_results[-8:], start=1):
        cleaned = result.strip()
        if not cleaned:
            continue
        block = f"[{index}]\n{cleaned}"
        clipped = _truncate_text(block, remaining)
        blocks.append(clipped)
        remaining -= len(clipped)
        if remaining <= 0:
            break
    return "\n\n".join(blocks) if blocks else "(no tool evidence captured)"


def _tool_call_signature(name: str, arguments: str) -> str:
    normalized_arguments = arguments.strip()
    try:
        parsed = json.loads(normalized_arguments) if normalized_arguments else {}
    except json.JSONDecodeError:
        parsed = normalized_arguments
    return f"{name}:{json.dumps(parsed, sort_keys=True, separators=(',', ':'))}"


def _truncate_text(text: str, char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    return text[: max(char_limit - 20, 0)].rstrip() + "\n[truncated]"


def _first_latency_metric(metrics: dict[str, int | str]) -> int:
    for key, value in metrics.items():
        if key.endswith("_ms") and isinstance(value, int):
            return value
    return 0


def _first_result_line(result: str) -> str:
    for line in result.strip().splitlines():
        cleaned = line.strip()
        if cleaned:
            return _truncate_text(cleaned, 120)
    return "(empty)"


def _write_agent_trace(metrics: dict[str, int | str] | None, trace: AgentTrace) -> None:
    if metrics is None:
        return
    rendered = trace.render()
    if rendered:
        metrics["agent_trace"] = rendered


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
            "YouTube transcript for:",
        )
    )


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
