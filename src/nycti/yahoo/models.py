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


@dataclass(frozen=True, slots=True)
class YahooMarketSnapshot:
    symbol: str
    currency: str | None = None
    exchange_name: str | None = None
    timezone_name: str | None = None
    market_state: str | None = None
    regular_price: float | None = None
    regular_previous_close: float | None = None
    regular_change: float | None = None
    regular_percent_change: float | None = None
    regular_timestamp: int | None = None
    market_cap: float | None = None
    shares_outstanding: float | None = None
    implied_shares_outstanding: float | None = None
    extended_price: float | None = None
    extended_timestamp: int | None = None
    extended_session: str | None = None


@dataclass(frozen=True, slots=True)
class YahooAnnualPerformanceYear:
    year: int
    start_date: str
    end_date: str
    start_price: float
    end_price: float
    price_change_percent: float
    distributions_per_share: float
    distribution_percent_of_start: float
    partial_year: bool = False


@dataclass(frozen=True, slots=True)
class YahooAnnualPerformance:
    requested_symbol: str
    symbol: str
    currency: str | None
    timezone_name: str | None
    years: tuple[YahooAnnualPerformanceYear, ...]
    source_url: str
