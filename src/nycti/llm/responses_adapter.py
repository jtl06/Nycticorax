from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from nycti.llm.tool_calls import LLMToolCall

RESPONSES_OUTPUT_ITEMS_KEY = "responses_output_items"
MISSING_FUNCTION_OUTPUT = (
    "Tool execution did not produce a result. Continue from the other available context."
)


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
    cached_prompt_tokens: int
    reasoning_tokens: int
    refusal: str
    incomplete_details: dict[str, object]
    response_output_items: list[dict[str, object]]


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
    service_tier: str | None = None,
) -> dict[str, object]:
    instructions = _instructions_from_messages(messages)
    converted_tools = _responses_tools(tools or [])
    response_input = _responses_input(messages)
    if instructions:
        response_input.insert(0, _cacheable_instructions(instructions))
    request: dict[str, object] = {
        "model": model,
        "input": response_input,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": max_tokens,
        "prompt_cache_key": _prompt_cache_key(
            model=model,
            instructions=instructions,
            tools=converted_tools,
        ),
        "store": False,
    }
    # openai-python 1.x accepts new GPT-5.6 cache controls through extra_body.
    # Implicit mode preserves growing tool-loop prefixes; the explicit marker
    # separately makes the static instructions reusable across Discord turns.
    request["extra_body"] = {
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"}
    }
    if service_tier:
        request["service_tier"] = service_tier
    if reasoning_effort:
        request["reasoning"] = {"effort": reasoning_effort}
    else:
        request["temperature"] = temperature
    if converted_tools:
        request["tools"] = converted_tools
        request["parallel_tool_calls"] = True
    return request


def parse_responses_turn(response: object, *, requested_model: str) -> ResponsesTurnData:
    status = str(_value(response, "status", "") or "")
    error = _value(response, "error")
    if status in {"failed", "cancelled"} or error is not None:
        raise RuntimeError(_response_error_message(status=status, error=error))

    text = str(_value(response, "output_text", "") or "").strip()
    tool_calls: list[LLMToolCall] = []
    reasoning_parts: list[str] = []
    message_text_parts: list[str] = []
    refusal_parts: list[str] = []
    response_output_items: list[dict[str, object]] = []
    for item in _value(response, "output", []) or []:
        plain_item = _plain_dict(item)
        if plain_item:
            response_output_items.append(plain_item)
        item_type = str(_value(item, "type", "") or "")
        if item_type == "function_call":
            call_id = str(_value(item, "call_id", "") or "")
            name = str(_value(item, "name", "") or "")
            if call_id and name:
                tool_calls.append(
                    LLMToolCall(
                        id=call_id,
                        name=name,
                        arguments=str(_value(item, "arguments", "") or ""),
                    )
                )
        elif item_type == "reasoning":
            reasoning_parts.extend(
                _text_parts(_value(item, "summary", []))
            )
            reasoning_parts.extend(_text_parts(_value(item, "content", [])))
        elif item_type == "message":
            for content_item in _value(item, "content", []) or []:
                content_type = str(_value(content_item, "type", "") or "")
                if content_type in {"output_text", "text"}:
                    content_text = str(_value(content_item, "text", "") or "").strip()
                    if content_text:
                        message_text_parts.append(content_text)
                elif content_type == "refusal":
                    refusal = str(_value(content_item, "refusal", "") or "").strip()
                    if refusal:
                        refusal_parts.append(refusal)
        elif item_type == "refusal":
            refusal = str(_value(item, "refusal", "") or "").strip()
            if refusal:
                refusal_parts.append(refusal)

    refusal = "\n\n".join(dict.fromkeys(refusal_parts))
    if not text:
        text = "\n\n".join(message_text_parts).strip() or refusal

    finish_reason = "tool_calls" if tool_calls else "stop"
    incomplete_details = _plain_dict(_value(response, "incomplete_details"))
    if status == "incomplete":
        incomplete_reason = str(incomplete_details.get("reason") or "").strip()
        finish_reason = "length" if incomplete_reason == "max_output_tokens" else (
            incomplete_reason or "incomplete"
        )
    usage = _value(response, "usage")
    prompt_tokens = int(_value(usage, "input_tokens", 0) or 0)
    completion_tokens = int(_value(usage, "output_tokens", 0) or 0)
    total_tokens = int(_value(usage, "total_tokens", 0) or 0)
    input_details = _value(usage, "input_tokens_details")
    output_details = _value(usage, "output_tokens_details")
    cached_prompt_tokens = int(_value(input_details, "cached_tokens", 0) or 0)
    reasoning_tokens = int(_value(output_details, "reasoning_tokens", 0) or 0)
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens
    return ResponsesTurnData(
        text=text,
        raw_text=text,
        tool_calls=tool_calls,
        reasoning_content="\n\n".join(reasoning_parts),
        finish_reason=finish_reason,
        model=str(_value(response, "model", "") or requested_model),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=cached_prompt_tokens,
        reasoning_tokens=reasoning_tokens,
        refusal=refusal,
        incomplete_details=incomplete_details,
        response_output_items=response_output_items,
    )


def _instructions_from_messages(messages: list[dict[str, object]]) -> str:
    return "\n\n".join(
        content.strip()
        for message in messages
        if message.get("role") in {"system", "developer"}
        and isinstance((content := message.get("content")), str)
        and content.strip()
    )


def _cacheable_instructions(instructions: str) -> dict[str, object]:
    return {
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": instructions,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ],
    }


def _prompt_cache_key(
    *,
    model: str,
    instructions: str,
    tools: list[dict[str, object]],
) -> str:
    stable_prefix = json.dumps(
        {
            "instructions": instructions,
            "model": model,
            "tools": tools,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()[:24]
    return f"nycti-agent-{digest}"


def _responses_input(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    pending_call_ids: dict[str, None] = {}
    emitted_call_ids: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if role in {"system", "developer"}:
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in pending_call_ids:
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": (
                            _string_content(message.get("content"))
                            or MISSING_FUNCTION_OUTPUT
                        ),
                    }
                )
                pending_call_ids.pop(call_id)
            continue
        _append_missing_function_outputs(result, pending_call_ids)
        if role == "assistant" and message.get(RESPONSES_OUTPUT_ITEMS_KEY):
            output_items = message.get(RESPONSES_OUTPUT_ITEMS_KEY)
            if isinstance(output_items, list):
                for item in output_items:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "")
                    if item_type == "function_call_output":
                        # These are input items, never assistant output items. Replaying
                        # one here could attach a result to the wrong model call.
                        continue
                    if item_type != "function_call":
                        result.append(item)
                        continue
                    function_call = _response_function_call(item)
                    if function_call is None:
                        continue
                    call_id = str(function_call["call_id"])
                    if call_id in emitted_call_ids:
                        continue
                    result.append(function_call)
                    emitted_call_ids.add(call_id)
                    pending_call_ids[call_id] = None
            continue
        if role == "assistant" and message.get("tool_calls"):
            content = _string_content(message.get("content"))
            if content:
                result.append({"role": "assistant", "content": content})
            _append_assistant_function_calls(
                result,
                pending_call_ids,
                emitted_call_ids,
                message.get("tool_calls"),
            )
            continue
        if role in {"user", "assistant"}:
            result.append(
                {
                    "role": role,
                    "content": _responses_content(message.get("content")),
                }
            )
    _append_missing_function_outputs(result, pending_call_ids)
    return result


def _append_assistant_function_calls(
    result: list[dict[str, object]],
    pending_call_ids: dict[str, None],
    emitted_call_ids: set[str],
    value: object,
) -> None:
    for function_call in _assistant_function_calls(value):
        call_id = str(function_call["call_id"])
        if call_id in emitted_call_ids:
            continue
        result.append(function_call)
        emitted_call_ids.add(call_id)
        pending_call_ids[call_id] = None


def _append_missing_function_outputs(
    result: list[dict[str, object]],
    pending_call_ids: dict[str, None],
) -> None:
    for call_id in pending_call_ids:
        result.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": MISSING_FUNCTION_OUTPUT,
            }
        )
    pending_call_ids.clear()


def _response_function_call(item: dict[str, object]) -> dict[str, object] | None:
    call_id = str(item.get("call_id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not call_id or not name:
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
    function_call = dict(item)
    function_call["type"] = "function_call"
    function_call["call_id"] = call_id
    function_call["name"] = name
    function_call["arguments"] = arguments
    return function_call


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


def _value(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _plain_dict(value: object) -> dict[str, object]:
    if value is None:
        return {}
    plain = _plain_value(value)
    return plain if isinstance(plain, dict) else {}


def _plain_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json", exclude_none=True)
        except TypeError:
            return model_dump(exclude_none=True)
    values = getattr(value, "__dict__", None)
    if isinstance(values, dict):
        return {
            str(key): _plain_value(item)
            for key, item in values.items()
            if item is not None and not str(key).startswith("_")
        }
    return str(value)


def _text_parts(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    parts: list[str] = []
    for item in value:
        text = str(_value(item, "text", "") or "").strip()
        if text:
            parts.append(text)
    return parts


def _response_error_message(*, status: str, error: object) -> str:
    code = str(_value(error, "code", "") or "").strip()
    message = str(_value(error, "message", "") or "").strip()
    if not message and error is not None:
        plain_error = _plain_value(error)
        message = json.dumps(plain_error, ensure_ascii=False, sort_keys=True, default=str)
    details = ": ".join(part for part in (code, message) if part)
    return f"Responses API failed ({status or 'error'})" + (f": {details}" if details else "")
