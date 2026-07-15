from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TwelveDataQuote:
    symbol: str
    name: str | None
    exchange: str | None
    instrument_type: str | None
    currency: str | None
    datetime: str | None
    close: float | None
    previous_close: float | None
    change: float | None
    percent_change: float | None
    high: float | None = None
    low: float | None = None
    open: float | None = None
    volume: int | None = None
    is_market_open: bool | None = None


@dataclass(frozen=True, slots=True)
class TwelveDataTimeSeriesPoint:
    datetime: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class TwelveDataTimeSeries:
    symbol: str
    name: str | None
    exchange: str | None
    instrument_type: str | None
    currency: str | None
    interval: str
    values: list[TwelveDataTimeSeriesPoint]


@dataclass(frozen=True, slots=True)
class TwelveDataPriceExtrema:
    symbol: str
    name: str | None
    exchange: str | None
    instrument_type: str | None
    currency: str | None
    coverage_start: str
    coverage_end: str
    candle_count: int
    highest_intraday: TwelveDataTimeSeriesPoint
    highest_close: TwelveDataTimeSeriesPoint
    lowest_intraday: TwelveDataTimeSeriesPoint
    latest: TwelveDataTimeSeriesPoint
    provider_request_count: int
    coverage_complete: bool


@dataclass(frozen=True, slots=True)
class TwelveDataSymbolMatch:
    symbol: str
    instrument_name: str | None
    exchange: str | None
    instrument_type: str | None
    country: str | None


class TwelveDataError(Exception):
    pass


class TwelveDataAPIKeyMissingError(TwelveDataError):
    pass


class TwelveDataHTTPError(TwelveDataError):
    pass


class TwelveDataDataError(TwelveDataError):
    pass
