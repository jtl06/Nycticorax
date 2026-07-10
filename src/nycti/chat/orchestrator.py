from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.finalization import continue_once_if_needed, finalize_run
from nycti.chat.loop_messages import (
    append_assistant_tool_call_message,
    append_skipped_tool_result,
    append_tool_outcomes,
)
from nycti.chat.model_runner import call_agent_model
from nycti.chat.orchestrator_support import (
    agent_output_budget,
    collect_reasoning,
    format_available_tool_guidance,
    increment_metric,
    looks_like_raw_tavily_dump,
    looks_like_tool_call_markup,
    quote_verification_prompt_for_price_answer,
    tool_call_signature,
    tool_names,
)
from nycti.chat.run_state import AgentBudget, AgentRun, AgentStep, StopReason, ToolStatus
from nycti.chat.run_telemetry import AgentRunTelemetryWriter, complete_agent_run
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tool_eligibility import (
    expand_tools_from_outcomes,
    select_eligible_tools,
)
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import build_chat_tools
if TYPE_CHECKING:
    import discord
    from nycti.browser import BrowserClient
    from nycti.channel_aliases import ChannelAliasService
    from nycti.config import Settings
    from nycti.db.session import Database
    from nycti.llm.client import LLMChatTurn, OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient
    from nycti.yahoo import YahooFinanceClient
    from nycti.youtube import YouTubeTranscriptClient

DEFAULT_AGENT_BUDGET = AgentBudget()


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
        self.llm_client = llm_client
        executor = ChatToolExecutor(
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
        self.tool_runner = ToolRunner(executor)
        self.telemetry_writer = AgentRunTelemetryWriter(database)
        self.agent_budget = DEFAULT_AGENT_BUDGET

    async def run_chat_with_tools(
        self,
        *,
        chat_model: str,
        messages: list[dict[str, object]],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        request_text: str,
        metrics: dict[str, int | str] | None,
        tool_runner: ToolRunner | None = None,
    ) -> tuple[str, list[str]]:
        eligible_tool_names, permissions = select_eligible_tools(
            request_text=request_text,
            guild_id=guild_id,
        )
        tools = build_chat_tools(eligible_tool_names)
        if metrics is not None:
            metrics["_diagnostic_tool_schemas_json"] = json.dumps(
                tools,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        available_tool_names = tool_names(tools)
        trace = AgentTrace(enabled=metrics is not None)
        run = AgentRun(
            messages=list(messages),
            budget=self.agent_budget,
            permissions=permissions,
        )
        reasoning_parts: list[str] = []
        output_budget = agent_output_budget(self.settings)
        active_tool_runner = tool_runner or self.tool_runner
        tool_guidance = format_available_tool_guidance(available_tool_names=available_tool_names)
        run.messages.append({"role": "user", "content": tool_guidance})
        if metrics is not None:
            metrics["agent_run_id"] = run.run_id
            metrics["tool_call_count"] = 0
            metrics["exposed_tool_count"] = len(available_tool_names)
            metrics["exposed_tools"] = ", ".join(sorted(available_tool_names)) or "(none)"

        while run.can_start_model_turn():
            run.step = AgentStep.MODEL
            try:
                turn = await self._call_model(
                    run=run,
                    chat_model=chat_model,
                    feature="chat_reply",
                    max_tokens=(
                        output_budget.tool_followup_tokens
                        if run.outcomes
                        else output_budget.reply_tokens
                    ),
                    temperature=0.4 if run.outcomes else 0.7,
                    tools=tools,
                    timeout_seconds=run.work_seconds_remaining(),
                    metrics=metrics,
                    trace=trace,
                )
            except TimeoutError:
                run.stop_reason = StopReason.DEADLINE
                break
            except Exception:
                increment_metric(metrics, "agent_provider_error_count")
                run.stop_reason = StopReason.PROVIDER_ERROR
                break
            reasoning_parts.extend(collect_reasoning(turn))

            if turn.tool_calls:
                append_assistant_tool_call_message(run.messages, turn)
                fresh_calls = []
                for tool_call in turn.tool_calls:
                    if tool_call.name not in available_tool_names:
                        append_skipped_tool_result(
                            run,
                            tool_call,
                            reason="Rejected because this tool was not authorized for the current request.",
                        )
                        increment_metric(metrics, "unauthorized_tool_call_count")
                        continue
                    signature = tool_call_signature(tool_call.name, tool_call.arguments)
                    if signature in run.seen_tool_signatures:
                        append_skipped_tool_result(
                            run,
                            tool_call,
                            reason="Skipped exact duplicate tool call; use the earlier result.",
                        )
                        increment_metric(metrics, "duplicate_tool_call_count")
                        continue
                    run.seen_tool_signatures.add(signature)
                    fresh_calls.append(tool_call)

                if not fresh_calls:
                    if run.use_correction():
                        run.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Those exact tool calls already ran. Use their existing results and answer, "
                                    "or call a materially different tool request."
                                ),
                            }
                        )
                        continue
                    run.stop_reason = StopReason.DUPLICATE_TOOL_CALL
                    break

                remaining = run.remaining_tool_calls()
                executable_calls = fresh_calls[:remaining]
                for skipped_call in fresh_calls[remaining:]:
                    append_skipped_tool_result(
                        run,
                        skipped_call,
                        reason="Skipped because the tool-call budget was exhausted.",
                    )
                if not executable_calls:
                    run.stop_reason = StopReason.TOOL_CALL_BUDGET
                    break

                run.step = AgentStep.TOOLS
                try:
                    outcomes = await asyncio.wait_for(
                        active_tool_runner.run(
                            executable_calls,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=user_id,
                            source_message_id=source_message_id,
                            permissions=run.permissions,
                            run_id=run.run_id,
                            step_index=run.model_turns,
                        ),
                        timeout=max(run.work_seconds_remaining(), 0.001),
                    )
                except TimeoutError:
                    run.stop_reason = StopReason.DEADLINE
                    break
                run.tool_calls += len(executable_calls)
                run.attempted_tools.update(tool_call.name for tool_call in executable_calls)
                run.successful_tools.update(
                    outcome.tool_name
                    for outcome in outcomes
                    if outcome.status == ToolStatus.OK
                )
                append_tool_outcomes(run, outcomes, metrics=metrics, trace=trace)
                for outcome in outcomes:
                    run.add_step_record(
                        state=AgentStep.TOOLS,
                        tool_name=outcome.tool_name,
                        argument_hash=hashlib.sha256(outcome.arguments.encode()).hexdigest(),
                        status=str(outcome.status),
                        latency_ms=outcome.latency_ms,
                        details={
                            "retryable": outcome.retryable,
                            "provenance": list(outcome.provenance),
                        },
                    )
                expanded_names = expand_tools_from_outcomes(available_tool_names, outcomes)
                if expanded_names != available_tool_names:
                    available_tool_names = expanded_names
                    tools = build_chat_tools(available_tool_names)
                    if metrics is not None:
                        metrics["exposed_tool_count"] = len(available_tool_names)
                        metrics["exposed_tools"] = ", ".join(sorted(available_tool_names))
                if run.remaining_tool_calls() == 0:
                    run.stop_reason = StopReason.TOOL_CALL_BUDGET
                    break
                continue

            if turn.text and not looks_like_raw_tavily_dump(turn.text) and not looks_like_tool_call_markup(turn.text):
                quote_verification_prompt = quote_verification_prompt_for_price_answer(
                    request_text=request_text,
                    answer_text=turn.text,
                    available_tool_names=available_tool_names,
                    used_tool_names=run.successful_tools,
                )
                if quote_verification_prompt and run.use_correction():
                    run.messages.append({"role": "user", "content": quote_verification_prompt})
                    increment_metric(metrics, "quote_verification_correction_count")
                    continue
                run.stop_reason = StopReason.FINAL_TEXT
                run.final_status = "success"
                answer, continuation_reasoning = await continue_once_if_needed(
                    run=run,
                    call_model=self._call_model,
                    chat_model=chat_model,
                    messages=run.messages,
                    initial_turn=turn,
                    initial_max_tokens=output_budget.reply_tokens,
                    continuation_max_tokens=output_budget.continuation_tokens,
                    metrics=metrics,
                    trace=trace,
                )
                reasoning_parts.extend(continuation_reasoning)
                return await complete_agent_run(
                    writer=getattr(self, "telemetry_writer", None),
                    run=run,
                    text=answer,
                    reasoning=reasoning_parts,
                    metrics=metrics,
                    trace=trace,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )

            increment_metric(metrics, "chat_empty_turn_count")
            if metrics is not None:
                metrics["chat_empty_turn_feature"] = "chat_reply"
            if run.use_correction():
                run.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Return either a native tool call or a concise final answer. "
                            "Do not return raw tool output or textual tool-call markup."
                        ),
                    }
                )
                continue
            run.stop_reason = StopReason.EMPTY_TURN
            break

        if run.stop_reason is None:
            run.stop_reason = (
                StopReason.DEADLINE
                if run.work_seconds_remaining() <= 0
                else StopReason.MODEL_TURN_BUDGET
            )
        final_text, final_reasoning = await finalize_run(
            run=run,
            call_model=self._call_model,
            chat_model=chat_model,
            final_max_tokens=output_budget.final_tokens,
            continuation_max_tokens=output_budget.continuation_tokens,
            metrics=metrics,
            trace=trace,
        )
        reasoning_parts.extend(final_reasoning)
        return await complete_agent_run(
            writer=getattr(self, "telemetry_writer", None),
            run=run,
            text=final_text,
            reasoning=reasoning_parts,
            metrics=metrics,
            trace=trace,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )

    async def _call_model(
        self,
        *,
        run: AgentRun,
        chat_model: str,
        feature: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, object]] | None,
        timeout_seconds: float,
        metrics: dict[str, int | str] | None,
        trace: AgentTrace,
    ) -> LLMChatTurn:
        return await call_agent_model(
            llm_client=self.llm_client,
            run=run,
            chat_model=chat_model,
            feature=feature,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            timeout_seconds=timeout_seconds,
            metrics=metrics,
            trace=trace,
        )
