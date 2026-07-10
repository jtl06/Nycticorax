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
class BrowserExtractToolArguments:
    url: str
    query: str | None
    headed: bool


@dataclass(frozen=True, slots=True)
class YouTubeTranscriptToolArguments:
    url: str
    query: str | None


@dataclass(frozen=True, slots=True)
class PriceHistoryToolArguments:
    symbol: str
    interval: str
    outputsize: int
    start_date: str | None
    end_date: str | None


@dataclass(frozen=True, slots=True)
class AnnualPerformanceToolArguments:
    symbols: tuple[str, ...]
    start_year: int | None


@dataclass(frozen=True, slots=True)
class ChannelContextToolArguments:
    mode: str
    multiplier: int
    expand: bool


@dataclass(frozen=True, slots=True)
class WebSearchToolArguments:
    queries: tuple[str, ...]
    topic: str | None
    time_range: str | None


@dataclass(frozen=True, slots=True)
class DeepResearchToolArguments:
    question: str
    focus: str | None
    urls: tuple[str, ...]
    symbols: tuple[str, ...]
    youtube_urls: tuple[str, ...]
    calculations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemorySearchToolArguments:
    query: str
    owner_user_ids: tuple[int, ...] | None
    visibility_scopes: tuple[str, ...] | None


def parse_tool_query_argument(arguments: str, *, field: str = "query") -> str | None:
    payload = _parse_required_string_fields(arguments, field)
    if payload is None:
        return None
    return payload[field]


def parse_tool_query_list_arguments(
    arguments: str,
    *,
    field: str = "query",
    alternate_field: str = "queries",
    max_items: int = 4,
) -> list[str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    queries: list[str] = []
    raw_queries = payload.get(alternate_field)
    if isinstance(raw_queries, list):
        queries.extend(str(query).strip() for query in raw_queries if query is not None)
    raw_query = _optional_string(payload, field) or ""
    if raw_query:
        queries.append(raw_query)
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = " ".join(query.split())
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
        if len(normalized) >= max_items:
            break
    return normalized or None


def parse_web_search_arguments(arguments: str, *, max_items: int = 4) -> WebSearchToolArguments | None:
    payload = parse_json_object_payload(arguments)
    queries = parse_tool_query_list_arguments(arguments, max_items=max_items)
    if payload is None or not queries:
        return None
    topic = (_optional_string(payload, "topic") or "").casefold() or None
    time_range = (_optional_string(payload, "time_range") or "").casefold() or None
    if topic not in {None, "general", "news", "finance"}:
        return None
    if time_range not in {None, "day", "week", "month", "year"}:
        return None
    return WebSearchToolArguments(
        queries=tuple(queries),
        topic=topic,
        time_range=time_range,
    )


def parse_deep_research_arguments(arguments: str) -> DeepResearchToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    question = _optional_string(payload, "question") or ""
    if not question:
        return None
    focus = _optional_string(payload, "focus")
    urls_valid, urls = _optional_string_list(payload, "urls", max_items=3, max_chars=2_000)
    symbols_valid, symbols = _optional_string_list(
        payload,
        "symbols",
        max_items=5,
        max_chars=24,
    )
    youtube_urls_valid, youtube_urls = _optional_string_list(
        payload,
        "youtube_urls",
        max_items=2,
        max_chars=2_000,
    )
    calculations_valid, calculations = _optional_string_list(
        payload,
        "calculations",
        max_items=2,
        max_chars=2_000,
    )
    if not all((urls_valid, symbols_valid, youtube_urls_valid, calculations_valid)):
        return None
    return DeepResearchToolArguments(
        question=question[:4_000],
        focus=focus[:500] if focus else None,
        urls=tuple(urls),
        symbols=tuple(value.upper() for value in symbols),
        youtube_urls=tuple(youtube_urls),
        calculations=tuple(calculations),
    )


def parse_memory_search_arguments(arguments: str) -> MemorySearchToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    query = _optional_string(payload, "query") or ""
    if not query:
        return None

    raw_owner_ids = payload.get("owner_user_ids")
    owner_user_ids: tuple[int, ...] | None = None
    if raw_owner_ids is not None:
        if not isinstance(raw_owner_ids, list) or len(raw_owner_ids) > 8:
            return None
        parsed_owner_ids: list[int] = []
        for value in raw_owner_ids:
            try:
                owner_user_id = int(value)
            except (TypeError, ValueError):
                return None
            if owner_user_id <= 0:
                return None
            if owner_user_id not in parsed_owner_ids:
                parsed_owner_ids.append(owner_user_id)
        owner_user_ids = tuple(parsed_owner_ids)

    raw_scopes = payload.get("visibility_scopes")
    visibility_scopes: tuple[str, ...] | None = None
    if raw_scopes is not None:
        if not isinstance(raw_scopes, list) or len(raw_scopes) > 3:
            return None
        allowed_scopes = {"private", "guild_shared", "lore"}
        parsed_scopes: list[str] = []
        for value in raw_scopes:
            if not isinstance(value, str):
                return None
            normalized = value.strip().casefold()
            if normalized not in allowed_scopes:
                return None
            if normalized not in parsed_scopes:
                parsed_scopes.append(normalized)
        visibility_scopes = tuple(parsed_scopes)

    return MemorySearchToolArguments(
        query=" ".join(query.split())[:2_000],
        owner_user_ids=owner_user_ids,
        visibility_scopes=visibility_scopes,
    )


def parse_tool_symbol_list_arguments(
    arguments: str,
    *,
    field: str = "symbol",
    alternate_field: str = "symbols",
    max_items: int = 10,
) -> list[str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None

    symbols: list[str] = []
    raw_symbols = payload.get(alternate_field)
    if isinstance(raw_symbols, list):
        for item in raw_symbols:
            if item is None:
                continue
            value = str(item).strip()
            if value:
                symbols.extend(_split_symbol_tokens(value))

    raw_symbol = _optional_string(payload, field) or ""
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
    symbol = (_optional_string(payload, "symbol") or "").upper()
    if not symbol:
        return None
    interval = _optional_string(payload, "interval") or "1day"
    outputsize_raw = _optional_string(payload, "outputsize") or ""
    if outputsize_raw:
        try:
            outputsize = int(outputsize_raw)
        except ValueError:
            return None
        if outputsize < 1 or outputsize > 30:
            return None
    else:
        outputsize = 5
    start_date = _optional_string(payload, "start_date")
    end_date = _optional_string(payload, "end_date")
    return PriceHistoryToolArguments(
        symbol=symbol,
        interval=interval,
        outputsize=outputsize,
        start_date=start_date,
        end_date=end_date,
    )


def parse_annual_performance_arguments(arguments: str) -> AnnualPerformanceToolArguments | None:
    payload = parse_json_object_payload(arguments)
    symbols = parse_tool_symbol_list_arguments(arguments, max_items=5)
    if payload is None or not symbols:
        return None
    raw_start_year = _optional_string(payload, "start_year") or ""
    if raw_start_year:
        try:
            start_year = int(raw_start_year)
        except ValueError:
            return None
        if start_year < 1970 or start_year > 2100:
            return None
    else:
        start_year = None
    return AnnualPerformanceToolArguments(symbols=tuple(symbols), start_year=start_year)


def parse_channel_context_arguments(arguments: str) -> ChannelContextToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    mode = (_optional_string(payload, "mode") or "").lower()
    if mode not in {"raw", "summary"}:
        return None
    multiplier_raw = _optional_string(payload, "multiplier") or ""
    if multiplier_raw:
        try:
            multiplier = int(multiplier_raw)
        except ValueError:
            return None
    else:
        multiplier = 1
    if multiplier < 1 or multiplier > 3:
        return None
    expand_raw = payload.get("expand", False)
    if isinstance(expand_raw, bool):
        expand = expand_raw
    elif isinstance(expand_raw, str):
        normalized = expand_raw.strip().lower()
        if normalized in {"true", "1", "yes"}:
            expand = True
        elif normalized in {"false", "0", "no", ""}:
            expand = False
        else:
            return None
    elif expand_raw is None:
        expand = False
    else:
        return None
    return ChannelContextToolArguments(mode=mode, multiplier=multiplier, expand=expand)


def parse_python_exec_arguments(arguments: str) -> str | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    code = payload.get("code")
    if not isinstance(code, str):
        return None
    cleaned = code.strip()
    if not cleaned:
        return None
    return cleaned[:6000]


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
    url = _optional_string(payload, "url") or ""
    if not url:
        return None
    query = _optional_string(payload, "query")
    return UrlExtractToolArguments(url=url, query=query)


def parse_browser_extract_arguments(arguments: str) -> BrowserExtractToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    url = _optional_string(payload, "url") or ""
    if not url:
        return None
    query = _optional_string(payload, "query")
    headed_raw = payload.get("headed", False)
    if isinstance(headed_raw, bool):
        headed = headed_raw
    elif isinstance(headed_raw, str):
        normalized = headed_raw.strip().lower()
        if normalized in {"true", "1", "yes"}:
            headed = True
        elif normalized in {"false", "0", "no", ""}:
            headed = False
        else:
            return None
    elif headed_raw is None:
        headed = False
    else:
        return None
    return BrowserExtractToolArguments(url=url, query=query, headed=headed)


def parse_youtube_transcript_arguments(arguments: str) -> YouTubeTranscriptToolArguments | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None
    url = _optional_string(payload, "url") or ""
    if not url:
        return None
    query = _optional_string(payload, "query")
    return YouTubeTranscriptToolArguments(url=url, query=query)


def _parse_required_string_fields(arguments: str, *fields: str) -> dict[str, str] | None:
    payload = parse_json_object_payload(arguments)
    if payload is None:
        return None

    parsed: dict[str, str] = {}
    for field in fields:
        value = _optional_string(payload, field)
        if not value:
            return None
        parsed[field] = value
    return parsed


def _optional_string(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    return str(value).strip() or None


def _optional_string_list(
    payload: dict[str, object],
    field: str,
    *,
    max_items: int,
    max_chars: int,
) -> tuple[bool, list[str]]:
    """Parse a nullable bounded string list and return (valid, values)."""

    raw_values = payload.get(field)
    if raw_values is None:
        return True, []
    if not isinstance(raw_values, list) or len(raw_values) > max_items:
        return False, []
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            return False, []
        value = raw_value.strip()
        if not value:
            continue
        value = value[:max_chars]
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return True, values


def _split_symbol_tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[\s,]+", value) if token.strip()]
