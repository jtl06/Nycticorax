from __future__ import annotations

import asyncio
from functools import partial
import hashlib
import time
from typing import TYPE_CHECKING

from nycti.agent_trace import AgentTrace
from nycti.chat.evidence_enforcement import (
    append_evidence_guidance,
    prepare_answer_for_delivery,
    request_evidence_repair,
)
from nycti.chat.deep_research_integration import (
    build_composite_deep_research_service as build_deep_research_service,
)
from nycti.chat.finalization import continue_once_if_needed, finalize_run
from nycti.chat.loop_messages import (
    append_assistant_tool_call_message,
    append_skipped_tool_result,
    append_tool_outcomes,
)
from nycti.chat.model_runner import call_agent_model
from nycti.chat.orchestrator_support import (
    agent_output_budget,
    answer_model_for_profile,
    collect_reasoning,
    constrain_answer_plan_to_runtime,
    format_available_tool_guidance,
    format_tool_schemas,
    increment_metric,
    looks_like_raw_tavily_dump,
    looks_like_tool_call_markup,
    quote_verification_prompt_for_price_answer,
    record_output_budget_metrics, tool_call_signature,
    tool_names,
)
from nycti.chat.run_state import (
    AgentBudget,
    AgentRun,
    AgentStep,
    AnswerProfile,
    CorrectionKind,
    StopReason,
    ToolStatus,
)
from nycti.chat.run_telemetry import AgentRunTelemetryWriter, complete_agent_run
from nycti.chat.tool_budget import select_tool_calls_for_run
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tool_eligibility import expand_tools_from_outcomes, select_answer_plan
from nycti.chat.tools.executor import ChatToolExecutor
from nycti.chat.tools.schemas import build_chat_tools
from nycti.llm.responses_adapter import should_use_responses_api
if TYPE_CHECKING:
    import discord
    from nycti.browser import BrowserClient
    from nycti.channel_aliases import ChannelAliasService
    from nycti.config import Settings
    from nycti.db.session import Database
    from nycti.llm.client import OpenAIClient
    from nycti.memory.service import MemoryService
    from nycti.reminders.service import ReminderService
    from nycti.tavily.client import TavilyClient
    from nycti.twelvedata.client import TwelveDataClient
    from nycti.yahoo import YahooFinanceClient
    from nycti.youtube import YouTubeTranscriptClient

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
        self.deep_research_service = build_deep_research_service(settings, llm_client, tavily_client)
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
            deep_research_service=self.deep_research_service,
            bot=bot,
        )
        self.tool_runner = ToolRunner(executor)
        self.telemetry_writer = AgentRunTelemetryWriter(database)
        self.agent_budget = AgentBudget()

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
        depth_override: AnswerProfile | str | None = None,
        request_started_at: float | None = None,
    ) -> tuple[str, list[str]]:
        answer_plan, permissions = select_answer_plan(
            request_text=request_text,
            guild_id=guild_id,
            default_budget=self.agent_budget,
            depth_override=depth_override,
        )
        active_tool_runner = tool_runner or self.tool_runner
        answer_plan = constrain_answer_plan_to_runtime(
            answer_plan, active_tool_runner,
            guild_id=guild_id, channel_id=channel_id, source_message_id=source_message_id,
        )
        chat_model = answer_model_for_profile(self.settings, answer_plan.profile, chat_model)
        tools = build_chat_tools(answer_plan.eligible_tool_names)
        available_tool_names = tool_names(tools)
        trace = AgentTrace(enabled=metrics is not None)
        call_model = partial(call_agent_model, llm_client=self.llm_client)
        run = AgentRun(
            messages=list(messages),
            budget=answer_plan.budget,
            permissions=permissions,
            answer_plan=answer_plan,
            started_at=request_started_at if request_started_at is not None else time.perf_counter(),
        )
        reasoning_parts: list[str] = []
        hidden_reasoning_effort = None
        if should_use_responses_api(
            provider_name=str(
                getattr(getattr(self.llm_client, "provider_capabilities", None), "name", "")
            ),
            model=chat_model,
        ):
            hidden_reasoning_effort = answer_plan.reasoning_effort_override or str(
                getattr(self.settings, "openai_reasoning_effort", "") or ""
            )
        output_budget = agent_output_budget(self.settings, answer_plan.profile,
            hidden_reasoning_effort=hidden_reasoning_effort,
        )
        record_output_budget_metrics(metrics, output_budget)
        if available_tool_names:
            tool_guidance = format_available_tool_guidance(
                available_tool_names=available_tool_names, answer_profile=answer_plan.profile,
                promoted_tool_names=answer_plan.promoted_tool_names,
            )
            run.messages.append({"role": "user", "content": tool_guidance})
        if metrics is not None:
            metrics["_diagnostic_tool_schemas_json"] = format_tool_schemas(tools)
            metrics["chat_model"] = chat_model
            metrics["agent_run_id"] = run.run_id
            metrics["answer_profile"] = str(answer_plan.profile)
            metrics["answer_profile_reason"] = answer_plan.selection_reason
            metrics["answer_profile_explicit"] = "yes" if answer_plan.explicit_override else "no"
            metrics["answer_reasoning_effort_override"] = (
                answer_plan.reasoning_effort_override or "configured-default"
            )
            metrics["answer_timeout_seconds"] = str(answer_plan.budget.total_timeout_seconds)
            metrics.setdefault("tool_call_count", 0)
            metrics["exposed_tool_count"] = len(available_tool_names)
            metrics["exposed_tools"] = ", ".join(sorted(available_tool_names)) or "(none)"

        while run.can_start_model_turn():
            run.step = AgentStep.MODEL
            try:
                turn = await call_model(
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
                    if run.use_correction(CorrectionKind.DUPLICATE_TOOL):
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

                budget_selection = select_tool_calls_for_run(fresh_calls, run)
                executable_calls = list(budget_selection.executable)
                for skipped_call, skip_reason in budget_selection.skipped:
                    append_skipped_tool_result(
                        run,
                        skipped_call,
                        reason=skip_reason,
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
                budget_selection.record_execution(run)
                run.attempted_tools.update(tool_call.name for tool_call in executable_calls)
                run.successful_tools.update(
                    outcome.tool_name
                    for outcome in outcomes
                    if outcome.status == ToolStatus.OK
                )
                append_tool_outcomes(run, outcomes, metrics=metrics, trace=trace)
                append_evidence_guidance(run, metrics=metrics)
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
                expanded_names = expand_tools_from_outcomes(
                    available_tool_names, outcomes, reachable_tool_names=answer_plan.reachable_tool_names,
                )
                if expanded_names != available_tool_names:
                    available_tool_names = expanded_names
                    tools = build_chat_tools(available_tool_names)
                    if metrics is not None:
                        metrics["exposed_tool_count"] = len(available_tool_names)
                        metrics["exposed_tools"] = ", ".join(sorted(available_tool_names))
                if run.remaining_tool_cost_units() == 0:
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
                if quote_verification_prompt and run.use_correction(CorrectionKind.QUOTE_VERIFICATION):
                    append_assistant_tool_call_message(run.messages, turn)
                    run.messages.append({"role": "user", "content": quote_verification_prompt})
                    increment_metric(metrics, "quote_verification_correction_count")
                    continue
                if request_evidence_repair(run, turn, metrics=metrics):
                    continue
                run.stop_reason = StopReason.FINAL_TEXT
                run.final_status = "success"
                answer, continuation_reasoning = await continue_once_if_needed(
                    run=run,
                    call_model=call_model,
                    chat_model=chat_model,
                    messages=run.messages,
                    initial_turn=turn,
                    initial_max_tokens=output_budget.reply_tokens,
                    continuation_max_tokens=output_budget.continuation_tokens,
                    metrics=metrics,
                    trace=trace,
                )
                reasoning_parts.extend(continuation_reasoning)
                answer = prepare_answer_for_delivery(run, answer, metrics=metrics)
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
            if run.use_correction(CorrectionKind.EMPTY_TURN):
                if turn.text or getattr(turn, "response_output_items", []):
                    append_assistant_tool_call_message(run.messages, turn)
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
            call_model=call_model,
            chat_model=chat_model,
            final_max_tokens=output_budget.final_tokens,
            continuation_max_tokens=output_budget.continuation_tokens,
            metrics=metrics,
            trace=trace,
        )
        reasoning_parts.extend(final_reasoning)
        final_text = prepare_answer_for_delivery(run, final_text, metrics=metrics)
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
