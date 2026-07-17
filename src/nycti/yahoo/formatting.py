from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nycti.yahoo.models import YahooExtendedHoursQuote, YahooMarketSnapshot


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


def format_yahoo_market_snapshot_message(snapshot: YahooMarketSnapshot) -> str:
    if (
        snapshot.market_cap is None
        and snapshot.shares_outstanding is None
        and snapshot.implied_shares_outstanding is None
    ):
        return ""
    header_parts = [snapshot.symbol]
    if snapshot.exchange_name:
        header_parts.append(snapshot.exchange_name)
    currency_prefix = f"{snapshot.currency} " if snapshot.currency else ""
    lines = [
        f"Yahoo Finance public-company valuation for: {' | '.join(header_parts)}"
    ]
    if snapshot.market_cap is not None:
        lines.append(
            "Market cap (regular-price basis): "
            f"{currency_prefix}{_format_compact_number(snapshot.market_cap)}"
        )
    if snapshot.shares_outstanding is not None:
        lines.append(
            "Shares outstanding: "
            f"{_format_compact_number(snapshot.shares_outstanding)}"
        )
    if (
        snapshot.implied_shares_outstanding is not None
        and snapshot.implied_shares_outstanding != snapshot.shares_outstanding
    ):
        lines.append(
            "Implied shares outstanding: "
            f"{_format_compact_number(snapshot.implied_shares_outstanding)}"
        )
    if snapshot.regular_timestamp is not None:
        lines.append(
            "Valuation quote time: "
            f"{_format_timestamp(snapshot.regular_timestamp, snapshot.timezone_name)}"
        )
    return "\n".join(lines)


def yahoo_extended_hours_from_snapshot(
    snapshot: YahooMarketSnapshot,
) -> YahooExtendedHoursQuote | None:
    if (
        snapshot.extended_price is None
        or snapshot.extended_timestamp is None
        or snapshot.extended_session is None
    ):
        return None
    return YahooExtendedHoursQuote(
        symbol=snapshot.symbol,
        price=snapshot.extended_price,
        timestamp=snapshot.extended_timestamp,
        session=snapshot.extended_session,
        currency=snapshot.currency,
        exchange_name=snapshot.exchange_name,
        timezone_name=snapshot.timezone_name,
        market_state=snapshot.market_state,
        regular_price=snapshot.regular_price,
        regular_previous_close=snapshot.regular_previous_close,
        regular_change=snapshot.regular_change,
        regular_percent_change=snapshot.regular_percent_change,
        regular_timestamp=snapshot.regular_timestamp,
    )


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


def _format_compact_number(value: float) -> str:
    for divisor, suffix in (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
    ):
        if abs(value) >= divisor:
            return f"{value / divisor:.4f}{suffix}"
    return f"{value:,.0f}"
