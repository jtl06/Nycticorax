from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nycti.yahoo.models import YahooExtendedHoursQuote


def format_yahoo_extended_hours_message(
    quote: YahooExtendedHoursQuote,
    *,
    regular_close: float | None,
) -> str:
    session_label = "Pre-market" if quote.session == "pre" else "After-hours"
    header_parts = [quote.symbol]
    if quote.exchange_name:
        header_parts.append(quote.exchange_name)
    lines = [f"Yahoo Finance extended-hours fallback for: {' | '.join(header_parts)}"]
    currency_prefix = f"{quote.currency} " if quote.currency else ""
    lines.append(f"{session_label} price: {currency_prefix}{quote.price:.4f}")
    lines.append(f"Quote time: {_format_timestamp(quote.timestamp, quote.timezone_name)}")
    if regular_close is not None:
        change = quote.price - regular_close
        parts = [f"{change:+.4f}"]
        if regular_close:
            parts.append(f"({change / regular_close * 100:+.2f}%)")
        parts.append(f"vs Twelve Data close {regular_close:.4f}")
        lines.append("Extended-hours change: " + " ".join(parts))
    if quote.market_state:
        lines.append(f"Yahoo market state: {quote.market_state}")
    return "\n".join(lines)


def _format_timestamp(timestamp: int, timezone_name: str | None) -> str:
    tz = timezone.utc
    if timezone_name:
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            tz = timezone.utc
    rendered = datetime.fromtimestamp(timestamp, tz=tz)
    return rendered.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
