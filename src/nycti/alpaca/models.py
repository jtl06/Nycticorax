from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AlpacaTrade:
    price: float
    timestamp: str
    size: int | None = None
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class AlpacaQuote:
    bid_price: float
    ask_price: float
    bid_size: int | None = None
    ask_size: int | None = None
    timestamp: str = ""


@dataclass(frozen=True, slots=True)
class AlpacaBar:
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    timestamp: str = ""


@dataclass(frozen=True, slots=True)
class AlpacaStockSnapshot:
    symbol: str
    latest_trade: AlpacaTrade | None
    latest_quote: AlpacaQuote | None
    daily_bar: AlpacaBar | None
    previous_daily_bar: AlpacaBar | None
    feed: str


class AlpacaError(Exception):
    pass


class AlpacaAPIKeyMissingError(AlpacaError):
    pass


class AlpacaHTTPError(AlpacaError):
    pass


class AlpacaDataError(AlpacaError):
    pass
