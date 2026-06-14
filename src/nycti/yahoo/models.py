from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class YahooExtendedHoursQuote:
    symbol: str
    price: float
    timestamp: int
    session: str
    currency: str | None = None
    exchange_name: str | None = None
    timezone_name: str | None = None
    market_state: str | None = None
    regular_price: float | None = None
    regular_previous_close: float | None = None
    regular_change: float | None = None
    regular_percent_change: float | None = None
    regular_timestamp: int | None = None
