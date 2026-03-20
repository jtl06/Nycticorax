from __future__ import annotations

from dataclasses import dataclass

from nycti.formatting import parse_json_object_payload


@dataclass(frozen=True, slots=True)
class ReminderToolArguments:
    message: str
    remind_at: str


@dataclass(frozen=True, slots=True)
class ChannelMessageToolArguments:
    channel: str
    message: str


@dataclass(frozen=True, slots=True)
class UrlExtractToolArguments:
    url: str
    query: str | None


def parse_tool_query_argument(arguments: str, *, field: str = "query") -> str | None:
    payload = _parse_required_string_fields(arguments, field)
    if payload is None:
        return None
    return payload[field]


def parse_create_reminder_arguments(arguments: str) -> ReminderToolArguments | None:
    payload = _parse_required_string_fields(arguments, "message", "remind_at")
    if payload is None:
        return None
    return ReminderToolArguments(
        message=payload["message"],
        remind_at=payload["remind_at"],
    )


def parse_send_channel_message_arguments(arguments: str) -> ChannelMessageToolArguments | None:
    payload = _parse_required_string_fields(arguments, "channel", "message")
    if payload is None:
        return None
    return ChannelMessageToolArguments(
        channel=payload["channel"],
        message=payload["message"],
    )


def parse_extract_url_arguments(arguments: str) -> UrlExtractToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    url = str(payload.get("url", "")).strip()
    if not url:
        return None
    query = str(payload.get("query", "")).strip() or None
    return UrlExtractToolArguments(url=url, query=query)


def _parse_required_string_fields(arguments: str, *fields: str) -> dict[str, str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None

    parsed: dict[str, str] = {}
    for field in fields:
        value = str(payload.get(field, "")).strip()
        if not value:
            return None
        parsed[field] = value
    return parsed
