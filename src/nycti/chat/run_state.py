from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import time
from typing import Any
from uuid import uuid4


class AgentStep(StrEnum):
    MODEL = "model"
    TOOLS = "tools"
    FINALIZE = "finalize"
    DONE = "done"


class AnswerProfile(StrEnum):
    QUICK = "quick"
    GROUNDED = "grounded"
    DEEP = "deep"


class StopReason(StrEnum):
    FINAL_TEXT = "final_text"
    DUPLICATE_TOOL_CALL = "duplicate_tool_call"
    EMPTY_TURN = "empty_turn"
    MODEL_TURN_BUDGET = "model_turn_budget"
    TOOL_CALL_BUDGET = "tool_call_budget"
    DEADLINE = "deadline"
    PROVIDER_ERROR = "provider_error"


class ToolStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


@dataclass(slots=True)
class ToolExecutionResult:
    content: str
    status: ToolStatus
    metrics: dict[str, int | str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class AgentStepRecord:
    step_index: int
    state: AgentStep
    feature: str = ""
    requested_model: str = ""
    active_model: str = ""
    provider: str = ""
    attempt: int = 0
    tool_name: str = ""
    argument_hash: str = ""
    status: str = ""
    stop_reason: str = ""
    prompt_version: str = "agent-loop-v2"
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentPermissions:
    allow_reminders: bool = False
    allow_cross_channel_send: bool = False


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_model_turns: int = 6
    max_tool_calls: int = 12
    max_corrections: int = 1
    max_continuations: int = 1
    total_timeout_seconds: float = 45.0
    finalization_reserve_seconds: float = 8.0


@dataclass(frozen=True, slots=True)
class AnswerPlan:
    profile: AnswerProfile
    eligible_tool_names: frozenset[str]
    budget: AgentBudget
    reasoning_effort_override: str | None = None
    selection_reason: str = "ambiguous_default"
    explicit_override: bool = False


@dataclass(frozen=True, slots=True)
class AgentOutputBudget:
    reply_tokens: int
    tool_followup_tokens: int
    final_tokens: int
    continuation_tokens: int


@dataclass(slots=True)
class ToolOutcome:
    call_id: str
    tool_name: str
    arguments: str
    status: ToolStatus
    content: str
    metrics: dict[str, int | str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    retryable: bool = False
    latency_ms: int = 0

    def model_content(self) -> str:
        if self.content.strip():
            return self.content.strip()
        return f"{self.tool_name} returned no usable result."


@dataclass(slots=True)
class AgentRun:
    messages: list[dict[str, object]]
    budget: AgentBudget = field(default_factory=AgentBudget)
    permissions: AgentPermissions = field(default_factory=AgentPermissions)
    answer_plan: AnswerPlan | None = None
    run_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=time.perf_counter)
    step: AgentStep = AgentStep.MODEL
    stop_reason: StopReason | None = None
    model_turns: int = 0
    tool_calls: int = 0
    corrections: int = 0
    continuations: int = 0
    native_tools_enabled: bool = True
    seen_tool_signatures: set[str] = field(default_factory=set)
    attempted_tools: set[str] = field(default_factory=set)
    successful_tools: set[str] = field(default_factory=set)
    guided_evidence_ids: set[str] = field(default_factory=set)
    outcomes: list[ToolOutcome] = field(default_factory=list)
    step_records: list[AgentStepRecord] = field(default_factory=list)
    usage_records: list[object] = field(default_factory=list)
    final_status: str = "running"
    final_failure_reason: str = ""

    def work_seconds_remaining(self) -> float:
        deadline = self.started_at + self.budget.total_timeout_seconds
        return max(deadline - self.budget.finalization_reserve_seconds - time.perf_counter(), 0.0)

    def final_seconds_remaining(self) -> float:
        deadline = self.started_at + self.budget.total_timeout_seconds
        return max(deadline - time.perf_counter(), 0.0)

    def can_start_model_turn(self) -> bool:
        return self.model_turns < self.budget.max_model_turns and self.work_seconds_remaining() > 0

    def remaining_tool_calls(self) -> int:
        return max(self.budget.max_tool_calls - self.tool_calls, 0)

    def use_correction(self) -> bool:
        if self.corrections >= self.budget.max_corrections:
            return False
        self.corrections += 1
        return True

    def add_step_record(self, **values: Any) -> None:
        self.step_records.append(
            AgentStepRecord(
                step_index=len(self.step_records) + 1,
                **values,
            )
        )

    def elapsed_ms(self) -> int:
        return max(round((time.perf_counter() - self.started_at) * 1000), 0)
