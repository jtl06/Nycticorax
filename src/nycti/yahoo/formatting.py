from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nycti.yahoo.models import YahooExtendedHoursQuote


def format_yahoo_extended_hours_message(
    quote: YahooExtendedHoursQuote,
    *,
    regular_close: float | None,
) -> str:
    session_label = _session_label(quote.session)
    header_parts = [quote.symbol]
    if quote.exchange_name:
        header_parts.append(quote.exchange_name)
    lines = [f"Yahoo Finance extended-hours fallback for: {' | '.join(header_parts)}"]
    currency_prefix = f"{quote.currency} " if quote.currency else ""
    if quote.regular_price is not None:
        regular_parts = [f"{currency_prefix}{quote.regular_price:.4f}"]
        if quote.regular_change is not None:
            regular_parts.append(f"{quote.regular_change:+.4f}")
        if quote.regular_percent_change is not None:
            regular_parts.append(f"({quote.regular_percent_change:+.2f}%)")
        if quote.regular_previous_close is not None:
            regular_parts.append(f"vs prev close {quote.regular_previous_close:.4f}")
        lines.append("Regular close (Yahoo): " + " ".join(regular_parts))
    lines.append(f"{session_label} price: {currency_prefix}{quote.price:.4f}")
    lines.append(f"Quote time: {_format_timestamp(quote.timestamp, quote.timezone_name)}")
    effective_regular_close = quote.regular_price or regular_close
    if effective_regular_close is not None:
        change = quote.price - effective_regular_close
        parts = [f"{change:+.4f}"]
        if effective_regular_close:
            parts.append(f"({change / effective_regular_close * 100:+.2f}%)")
        source = "Yahoo regular close" if quote.regular_price is not None else "Twelve Data close"
        parts.append(f"vs {source} {effective_regular_close:.4f}")
        lines.append("Extended-hours change: " + " ".join(parts))
    if (
        quote.regular_price is not None
        and regular_close is not None
        and abs(quote.regular_price - regular_close) >= 0.01
    ):
        lines.append(
            f"Provider conflict: Twelve Data close {regular_close:.4f} differs from "
            f"Yahoo regular close {quote.regular_price:.4f}; prefer Yahoo for this session."
        )
    if quote.market_state:
        lines.append(f"Yahoo market state: {quote.market_state}")
    return "\n".join(lines)


def _session_label(session: str) -> str:
    if session == "pre":
        return "Pre-market"
    if session == "overnight":
        return "Overnight"
    return "After-hours"


def _format_timestamp(timestamp: int, timezone_name: str | None) -> str:
    tz = timezone.utc
    if timezone_name:
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            tz = timezone.utc
    rendered = datetime.fromtimestamp(timestamp, tz=tz)
    return rendered.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
