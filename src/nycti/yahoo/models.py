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
