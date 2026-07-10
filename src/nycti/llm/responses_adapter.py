from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nycti.llm.tool_calls import LLMToolCall


@dataclass(frozen=True, slots=True)
class ResponsesTurnData:
    text: str
    raw_text: str
    tool_calls: list[LLMToolCall]
    reasoning_content: str
    finish_reason: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def should_use_responses_api(*, provider_name: str, model: str) -> bool:
    return provider_name == "openai" and model.casefold().startswith("gpt-5.6")


def build_responses_request(
    *,
    model: str,
    messages: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
    reasoning_effort: str,
    tools: list[dict[str, object]] | None,
) -> dict[str, object]:
    instructions = _instructions_from_messages(messages)
    request: dict[str, object] = {
        "model": model,
        "input": _responses_input(messages),
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if instructions:
        request["instructions"] = instructions
    if reasoning_effort:
        request["reasoning"] = {"effort": reasoning_effort}
    else:
        request["temperature"] = temperature
    converted_tools = _responses_tools(tools or [])
    if converted_tools:
        request["tools"] = converted_tools
        request["parallel_tool_calls"] = True
    return request


def parse_responses_turn(response: object, *, requested_model: str) -> ResponsesTurnData:
    status = str(getattr(response, "status", "") or "")
    error = getattr(response, "error", None)
    if status == "failed" or error:
        raise RuntimeError(f"Responses API failed: {error or status}")

    text = str(getattr(response, "output_text", "") or "").strip()
    tool_calls: list[LLMToolCall] = []
    reasoning_parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        item_type = str(getattr(item, "type", "") or "")
        if item_type == "function_call":
            call_id = str(getattr(item, "call_id", "") or getattr(item, "id", "") or "")
            name = str(getattr(item, "name", "") or "")
            if call_id and name:
                tool_calls.append(
                    LLMToolCall(
                        id=call_id,
                        name=name,
                        arguments=str(getattr(item, "arguments", "") or ""),
                    )
                )
        elif item_type == "reasoning":
            reasoning_parts.extend(
                str(getattr(summary, "text", "") or "").strip()
                for summary in getattr(item, "summary", None) or []
                if str(getattr(summary, "text", "") or "").strip()
            )

    finish_reason = "tool_calls" if tool_calls else "stop"
    if status == "incomplete":
        finish_reason = "length"
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens
    return ResponsesTurnData(
        text=text,
        raw_text=text,
        tool_calls=tool_calls,
        reasoning_content="\n\n".join(reasoning_parts),
        finish_reason=finish_reason,
        model=str(getattr(response, "model", "") or requested_model),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _instructions_from_messages(messages: list[dict[str, object]]) -> str:
    return "\n\n".join(
        content.strip()
        for message in messages
        if message.get("role") in {"system", "developer"}
        and isinstance((content := message.get("content")), str)
        and content.strip()
    )


def _responses_input(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role in {"system", "developer"}:
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _string_content(message.get("content")),
                    }
                )
            continue
        if role == "assistant" and message.get("tool_calls"):
            content = _string_content(message.get("content"))
            if content:
                result.append({"role": "assistant", "content": content})
            result.extend(_assistant_function_calls(message.get("tool_calls")))
            continue
        if role in {"user", "assistant"}:
            result.append(
                {
                    "role": role,
                    "content": _responses_content(message.get("content")),
                }
            )
    return result


def _assistant_function_calls(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        call_id = str(item.get("id") or "")
        name = str(function.get("name") or "")
        if call_id and name:
            calls.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": str(function.get("arguments") or ""),
                }
            )
    return calls


def _responses_content(value: object) -> object:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return _string_content(value)
    converted: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image_url":
            image = item.get("image_url")
            if isinstance(image, dict) and image.get("url"):
                converted.append(
                    {
                        "type": "input_image",
                        "image_url": str(image["url"]),
                        "detail": str(image.get("detail") or "auto"),
                    }
                )
    return converted


def _responses_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name:
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object"},
                "strict": bool(function.get("strict", False)),
            }
        )
    return converted


def _string_content(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "").strip()
