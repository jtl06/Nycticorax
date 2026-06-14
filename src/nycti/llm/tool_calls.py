from __future__ import annotations

from dataclasses import dataclass
import json
import re
import xml.etree.ElementTree as ElementTree


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


INLINE_TOOL_SECTION_PATTERN = re.compile(
    r"<\|tool_calls_section_begin\|>(?P<body>.*?)<\|tool_calls_section_end\|>",
    flags=re.DOTALL,
)
INLINE_TOOL_CALL_PATTERN = re.compile(
    r"<\|tool_call_begin\|>\s*(?P<header>.*?)\s*<\|tool_call_argument_begin\|>"
    r"\s*(?P<arguments>.*?)\s*<\|tool_call_end\|>",
    flags=re.DOTALL,
)
XML_TOOL_SECTION_PATTERN = re.compile(
    r"<function_calls>\s*(?P<body>.*?)\s*</function_calls>",
    flags=re.DOTALL,
)


def _extract_inline_tool_calls(
    text: str,
    tools: list[dict[str, object]],
) -> tuple[str, list[LLMToolCall]]:
    available_names = _available_tool_names(tools)
    cleaned_text, calls = _extract_special_token_tool_calls(text, tools)
    if calls:
        return cleaned_text, calls
    if cleaned_text != text:
        return cleaned_text, []
    cleaned_text, calls = _extract_xml_tool_calls(text, available_names)
    if calls:
        return cleaned_text, calls
    if cleaned_text != text:
        return cleaned_text, []
    return _strip_inline_tool_call_markup(text), []


def _extract_special_token_tool_calls(
    text: str,
    tools: list[dict[str, object]],
) -> tuple[str, list[LLMToolCall]]:
    match = INLINE_TOOL_SECTION_PATTERN.search(text)
    if match is None:
        return text, []

    available_names = _available_tool_names(tools)
    calls: list[LLMToolCall] = []
    for index, call_match in enumerate(INLINE_TOOL_CALL_PATTERN.finditer(match.group("body")), start=1):
        header = " ".join(call_match.group("header").split())
        arguments = call_match.group("arguments").strip()
        tool_name = _extract_inline_tool_name(header, available_names)
        if not tool_name:
            tool_name = _infer_tool_name_from_arguments(arguments, tools)
        if not tool_name:
            continue
        calls.append(
            LLMToolCall(
                id=_extract_inline_tool_id(header, fallback_index=index),
                name=tool_name,
                arguments=arguments,
            )
        )

    cleaned_text = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned_text, calls


def _extract_xml_tool_calls(
    text: str,
    available_names: set[str],
) -> tuple[str, list[LLMToolCall]]:
    match = XML_TOOL_SECTION_PATTERN.search(text)
    if match is None:
        return text, []
    try:
        root = ElementTree.fromstring(match.group(0))
    except ElementTree.ParseError:
        return text, []
    calls: list[LLMToolCall] = []
    for index, invoke in enumerate(root.findall("invoke"), start=1):
        tool_name = str(invoke.attrib.get("name", "")).strip()
        if tool_name not in available_names:
            continue
        parameters = {
            name: "".join(parameter.itertext()).strip()
            for parameter in invoke.findall("parameter")
            if (name := str(parameter.attrib.get("name", "")).strip())
        }
        calls.append(
            LLMToolCall(
                id=f"call_xml_{index}",
                name=tool_name,
                arguments=json.dumps(parameters, separators=(",", ":")),
            )
        )
    cleaned_text = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned_text, calls


def _strip_inline_tool_call_markup(text: str) -> str:
    cleaned = INLINE_TOOL_SECTION_PATTERN.sub("", text)
    cleaned = XML_TOOL_SECTION_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"<\|tool_calls_section_begin\|>.*\Z", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<function_calls>.*\Z", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _available_tool_names(tools: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _extract_inline_tool_name(header: str, available_names: set[str]) -> str | None:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", header)
    for token in tokens:
        if token in available_names:
            return token
    explicit_name_tokens = [
        token
        for token in tokens
        if token not in {"functions", "function"} and not token.startswith("call_")
    ]
    if explicit_name_tokens:
        return None
    if len(available_names) == 1:
        return next(iter(available_names))
    return None


def _infer_tool_name_from_arguments(
    arguments: str,
    tools: list[dict[str, object]],
) -> str | None:
    try:
        payload = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload:
        return None

    argument_keys = set(payload)
    available_names = _available_tool_names(tools)
    preferred = _preferred_tool_for_argument_shape(payload, available_names)
    if preferred:
        return preferred

    candidates: list[str] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict) or not argument_keys <= set(properties):
            continue
        required = parameters.get("required", [])
        if isinstance(required, list) and not set(required) <= argument_keys:
            continue
        candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def _preferred_tool_for_argument_shape(
    payload: dict[str, object],
    available_names: set[str],
) -> str | None:
    keys = set(payload)
    preferences = (
        ("web", "queries" in keys),
        ("quote", "symbols" in keys),
        ("python", "code" in keys),
        ("channel_ctx", "mode" in keys),
        ("reminder", {"message", "remind_at"} <= keys),
        ("send_msg", {"channel", "message"} <= keys),
        ("price_hist", bool(keys & {"interval", "outputsize", "start_date", "end_date"})),
        ("browser_extract", "headed" in keys),
    )
    for name, matches in preferences:
        if matches and name in available_names:
            return name

    url = payload.get("url")
    if isinstance(url, str):
        normalized_url = url.casefold()
        if ("youtube.com" in normalized_url or "youtu.be" in normalized_url) and "yt_transcript" in available_names:
            return "yt_transcript"
        if "url_extract" in available_names:
            return "url_extract"
    return None


def _extract_inline_tool_id(header: str, fallback_index: int) -> str:
    match = re.search(r"\b(call_[A-Za-z0-9_-]+|call_\d+)\b", header)
    return match.group(1) if match is not None else f"call_{fallback_index}"
