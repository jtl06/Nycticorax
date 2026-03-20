from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE_NAME = "America/Los_Angeles"

TIMEZONE_ALIASES = {
    "PST": DEFAULT_TIMEZONE_NAME,
    "PDT": DEFAULT_TIMEZONE_NAME,
    "PT": DEFAULT_TIMEZONE_NAME,
    "PACIFIC": DEFAULT_TIMEZONE_NAME,
    "PACIFIC TIME": DEFAULT_TIMEZONE_NAME,
    "UTC": "UTC",
    "GMT": "UTC",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
}


def canonicalize_timezone_name(value: str) -> str | None:
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    candidate = TIMEZONE_ALIASES.get(cleaned.upper(), cleaned)
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return None
    return candidate


def resolve_timezone_name(value: str | None) -> str:
    if value:
        canonical = canonicalize_timezone_name(value)
        if canonical is not None:
            return canonical
    return DEFAULT_TIMEZONE_NAME


def get_timezone(value: str | None) -> ZoneInfo:
    return ZoneInfo(resolve_timezone_name(value))
