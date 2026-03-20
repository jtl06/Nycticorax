from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

DEFAULT_DATE_ONLY_REMINDER_HOUR = 9


@dataclass(frozen=True, slots=True)
class ParsedReminderTime:
    remind_at: datetime
    assumed_time: bool


def parse_remind_at(value: str, *, now: datetime) -> ParsedReminderTime | None:
    cleaned = value.strip()
    if not cleaned:
        return None

    tzinfo = now.tzinfo
    if tzinfo is None:
        now = now.astimezone()
        tzinfo = now.tzinfo

    if "T" not in cleaned and " " not in cleaned:
        try:
            parsed_date = date.fromisoformat(cleaned)
        except ValueError:
            return None
        remind_at = datetime.combine(
            parsed_date,
            time(hour=DEFAULT_DATE_ONLY_REMINDER_HOUR),
            tzinfo=tzinfo,
        )
        return ParsedReminderTime(remind_at=remind_at, assumed_time=True)

    normalized = cleaned.replace("Z", "+00:00")
    try:
        remind_at = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=tzinfo)
    return ParsedReminderTime(remind_at=remind_at, assumed_time=False)
