"""Bounded quote collection and numeric Discord rendering, without model synthesis."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import time

from nycti.twelvedata.models import TwelveDataQuote
from nycti.yahoo.models import YahooMarketSnapshot

SESSION_LABELS = {"pre": "premarket", "post": "after-hours", "overnight": "overnight", "regular": "regular"}


@dataclass(frozen=True)
class QuoteRow:
    symbol: str
    price: float | None = None
    currency: str = ""
    percent: float | None = None
    session: str = ""
    basis: str = ""
    timestamp: str = ""
    source: str = ""
    unavailable: str = ""

    def render(self, *, include_timestamps: bool = False) -> str:
        if self.price is None:
            return f"- {self.symbol}: unavailable ({self.unavailable or 'no usable quote'})."
        direction = "\U0001f7e2" if self.percent is not None and self.percent > 0 else (
            "\U0001f534" if self.percent is not None and self.percent < 0 else "\u26aa"
        )
        change = f"{self.percent:+.2f}% {self.basis}" if self.percent is not None else "change unavailable"
        price = f"{self.currency} {self.price:,.2f}".strip()
        timestamp = f" | {self.timestamp}" if include_timestamps else ""
        return f"- {direction} {self.symbol}: {price} | {change} | {self.session}{timestamp} ({self.source})"


@dataclass(frozen=True)
class MarketReport:
    rows: tuple[QuoteRow, ...]
    requested_session: str

    @property
    def success_count(self) -> int:
        return sum(row.price is not None for row in self.rows)

    def render(self, *, include_timestamps: bool = False) -> str:
        label = SESSION_LABELS.get(self.requested_session, "latest available")
        return f"Quotes ({label}):\n" + "\n".join(
            row.render(include_timestamps=include_timestamps) for row in self.rows
        )


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _percent(price: float, base: float | None, supplied: float | None = None) -> float | None:
    if _finite(base) and base > 0:
        calculated = (price / base - 1) * 100
        return calculated if _finite(calculated) else None
    return supplied if _finite(supplied) else None


def _currency(value: str | None) -> str:
    return value if value and re.fullmatch(r"[A-Z]{3}", value) else "currency unknown"


def _primary_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def _stamp(value: int | None, now: datetime) -> str:
    if not _finite(value) or value <= 0:
        return ""
    try:
        instant = datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return ""
    if (instant - now).total_seconds() > 300:
        return ""
    return instant.strftime("%Y-%m-%d %H:%M UTC")


def select_quote_row(
    symbol: str, primary: TwelveDataQuote | None, yahoo: YahooMarketSnapshot | None,
    *, requested_session: str, now: datetime,
) -> QuoteRow:
    primary_time = _primary_time(primary.datetime) if primary is not None else None
    # Never combine an extended price with another provider's closing-price basis.
    if yahoo is not None and yahoo.symbol.upper() == symbol.upper():
        ext_stamp = _stamp(yahoo.extended_timestamp, now)
        regular_stamp = _stamp(yahoo.regular_timestamp, now)
        newer = bool(
            ext_stamp and (not yahoo.regular_timestamp or yahoo.extended_timestamp > yahoo.regular_timestamp)
            and now.timestamp() - yahoo.extended_timestamp <= 18 * 3600
        )
        if (
            requested_session != "regular" and newer
            and yahoo.extended_session in {"pre", "post", "overnight"}
            and requested_session in {"latest", yahoo.extended_session}
            and _finite(yahoo.extended_price) and yahoo.extended_price > 0
        ):
            return QuoteRow(
                symbol, yahoo.extended_price, _currency(yahoo.currency),
                _percent(yahoo.extended_price, yahoo.regular_price),
                SESSION_LABELS[yahoo.extended_session], "vs regular close", ext_stamp, "Yahoo",
            )
        newer_primary_day = bool(
            primary_time and regular_stamp
            and primary_time.date() > datetime.fromtimestamp(yahoo.regular_timestamp, timezone.utc).date()
        )
        if requested_session in {"latest", "regular"} and not newer_primary_day and regular_stamp and _finite(yahoo.regular_price) and yahoo.regular_price > 0:
            return QuoteRow(
                symbol, yahoo.regular_price, _currency(yahoo.currency),
                _percent(yahoo.regular_price, yahoo.regular_previous_close, yahoo.regular_percent_change),
                "regular" if yahoo.market_state == "REGULAR" else "last regular",
                "vs prev close", regular_stamp, "Yahoo",
            )
    if requested_session not in {"latest", "regular"}:
        return QuoteRow(symbol, unavailable=f"no fresh {SESSION_LABELS[requested_session]} quote")
    if primary is not None and primary.symbol.upper() == symbol.upper() and _finite(primary.close) and primary.close > 0:
        stamp = primary_time.isoformat(sep=" ") if primary_time else "time unavailable"
        if primary_time and len(primary.datetime or "") <= 10:
            stamp = primary_time.date().isoformat() + " (time unavailable)"
        return QuoteRow(
            symbol, primary.close, _currency(primary.currency),
            _percent(primary.close, primary.previous_close, primary.percent_change),
            "regular" if primary.is_market_open else "last regular",
            "vs prev close", stamp, "Twelve Data",
        )
    return QuoteRow(symbol, unavailable="providers returned no matching price")


async def build_market_report(
    symbols: tuple[str, ...], *, requested_session: str, primary, yahoo,
    now: datetime | None = None,
) -> MarketReport:
    now = now or datetime.now(timezone.utc)
    semaphore = asyncio.Semaphore(10)
    deadline = time.monotonic() + 20.0

    async def bounded_call(client, method: str, symbol: str):
        if client is None:
            return None
        remaining = min(8.0, deadline - time.monotonic())
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(getattr(client, method)(symbol), timeout=remaining)
        except Exception:
            # Each provider/symbol fails independently; do not leak HTTP bodies or credentials.
            return None

    async def fetch(symbol: str) -> QuoteRow:
        async with semaphore:
            quote, snapshot = await asyncio.gather(
                bounded_call(primary, "get_market_quote", symbol),
                bounded_call(yahoo, "get_market_snapshot", symbol),
            )
            return select_quote_row(
                symbol, quote if isinstance(quote, TwelveDataQuote) else None,
                snapshot if isinstance(snapshot, YahooMarketSnapshot) else None,
                requested_session=requested_session, now=now,
            )

    rows = await asyncio.gather(*(fetch(symbol) for symbol in dict.fromkeys(symbols)))
    return MarketReport(tuple(rows), requested_session)
