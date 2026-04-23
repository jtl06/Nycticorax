from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from nycti.chat.tools.registry import TOOL_METADATA


@dataclass(frozen=True, slots=True)
class AgentEvalCase:
    name: str
    prompt: str
    should_use_tools: bool
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AgentEvalCaseError:
    name: str
    message: str


def load_agent_eval_cases(path: str | Path) -> list[AgentEvalCase]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("Agent eval cases file must contain a JSON array.")
    cases: list[AgentEvalCase] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each agent eval case must be an object.")
        cases.append(
            AgentEvalCase(
                name=str(item["name"]),
                prompt=str(item["prompt"]),
                should_use_tools=bool(item["should_use_tools"]),
                expected_tools=tuple(str(tool) for tool in item.get("expected_tools", [])),
                forbidden_tools=tuple(str(tool) for tool in item.get("forbidden_tools", [])),
                notes=str(item.get("notes", "")),
            )
        )
    return cases


def validate_agent_eval_cases(cases: Iterable[AgentEvalCase]) -> list[AgentEvalCaseError]:
    known_tools = set(TOOL_METADATA)
    errors: list[AgentEvalCaseError] = []
    names: set[str] = set()
    for case in cases:
        if not case.name.strip():
            errors.append(AgentEvalCaseError(case.name, "name is required"))
        if case.name in names:
            errors.append(AgentEvalCaseError(case.name, "duplicate case name"))
        names.add(case.name)
        if not case.prompt.strip():
            errors.append(AgentEvalCaseError(case.name, "prompt is required"))
        unknown_expected = sorted(set(case.expected_tools) - known_tools)
        if unknown_expected:
            errors.append(
                AgentEvalCaseError(case.name, f"unknown expected tools: {', '.join(unknown_expected)}")
            )
        unknown_forbidden = sorted(set(case.forbidden_tools) - known_tools)
        if unknown_forbidden:
            errors.append(
                AgentEvalCaseError(case.name, f"unknown forbidden tools: {', '.join(unknown_forbidden)}")
            )
        if not case.should_use_tools and case.expected_tools:
            errors.append(AgentEvalCaseError(case.name, "expected tools require should_use_tools=true"))
    return errors
