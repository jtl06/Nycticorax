from __future__ import annotations

from dataclasses import dataclass
import re

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


@dataclass(frozen=True, slots=True)
class PriceHistoryToolArguments:
    symbol: str
    interval: str
    outputsize: int
    start_date: str | None
    end_date: str | None


def parse_tool_query_argument(arguments: str, *, field: str = "query") -> str | None:
    payload = _parse_required_string_fields(arguments, field)
    if payload is None:
        return None
    return payload[field]


def parse_tool_symbol_list_arguments(
    arguments: str,
    *,
    field: str = "symbol",
    alternate_field: str = "symbols",
    max_items: int = 5,
) -> list[str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None

    symbols: list[str] = []
    raw_symbols = payload.get(alternate_field)
    if isinstance(raw_symbols, list):
        for item in raw_symbols:
            value = str(item).strip()
            if value:
                symbols.extend(_split_symbol_tokens(value))

    raw_symbol = str(payload.get(field, "")).strip()
    if raw_symbol:
        symbols.extend(_split_symbol_tokens(raw_symbol))

    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        upper = symbol.upper()
        if not upper or upper in seen:
            continue
        seen.add(upper)
        normalized.append(upper)
        if len(normalized) >= max_items:
            break
    return normalized or None


def parse_price_history_arguments(arguments: str) -> PriceHistoryToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not symbol:
        return None
    interval = str(payload.get("interval", "")).strip() or "1day"
    outputsize_raw = str(payload.get("outputsize", "")).strip()
    if outputsize_raw:
        try:
            outputsize = int(outputsize_raw)
        except ValueError:
            return None
        if outputsize < 1 or outputsize > 30:
            return None
    else:
        outputsize = 5
    start_date = str(payload.get("start_date", "")).strip() or None
    end_date = str(payload.get("end_date", "")).strip() or None
    return PriceHistoryToolArguments(
        symbol=symbol,
        interval=interval,
        outputsize=outputsize,
        start_date=start_date,
        end_date=end_date,
    )


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


def _split_symbol_tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[\s,]+", value) if token.strip()]
