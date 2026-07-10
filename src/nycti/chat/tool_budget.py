from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from nycti.chat.tools.registry import get_tool_spec
from nycti.chat.tools.schemas import DEEP_RESEARCH_TOOL_NAME

if TYPE_CHECKING:
    from nycti.chat.run_state import AgentRun


class BudgetedToolCall(Protocol):
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolBudgetSelection:
    executable: tuple[BudgetedToolCall, ...]
    skipped: tuple[tuple[BudgetedToolCall, str], ...]
    cost_units: int
    deep_research_calls: int

    def record_execution(self, run: AgentRun) -> None:
        run.tool_calls += len(self.executable)
        run.tool_cost_units += self.cost_units
        run.deep_research_calls += self.deep_research_calls


def select_tool_calls_for_run(
    calls: Sequence[BudgetedToolCall],
    run: AgentRun,
) -> ToolBudgetSelection:
    return select_tool_calls_within_budget(
        calls,
        remaining_cost_units=run.remaining_tool_cost_units(),
        remaining_deep_research_calls=run.remaining_deep_research_calls(),
    )


def select_tool_calls_within_budget(
    calls: Sequence[BudgetedToolCall],
    *,
    remaining_cost_units: int,
    remaining_deep_research_calls: int,
) -> ToolBudgetSelection:
    """Select a stable-order batch using server-owned cost and fan-out limits."""

    executable: list[BudgetedToolCall] = []
    skipped: list[tuple[BudgetedToolCall, str]] = []
    cost_units = 0
    deep_research_calls = 0
    available_units = max(remaining_cost_units, 0)
    available_deep_calls = max(remaining_deep_research_calls, 0)

    for call in calls:
        is_deep_research = call.name == DEEP_RESEARCH_TOOL_NAME
        if is_deep_research and deep_research_calls >= available_deep_calls:
            skipped.append(
                (call, "Skipped because the per-run deep-research limit was reached.")
            )
            continue

        spec = get_tool_spec(call.name)
        call_cost = max(spec.budget_cost_units if spec is not None else 1, 1)
        if cost_units + call_cost > available_units:
            skipped.append(
                (call, "Skipped because the weighted tool-call budget was exhausted.")
            )
            continue

        executable.append(call)
        cost_units += call_cost
        deep_research_calls += int(is_deep_research)

    return ToolBudgetSelection(
        executable=tuple(executable),
        skipped=tuple(skipped),
        cost_units=cost_units,
        deep_research_calls=deep_research_calls,
    )
