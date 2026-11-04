"""Normalize bounded Yahoo candles into the shared history processor's input."""
from collections.abc import Mapping
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from nycti.twelvedata.models import TwelveDataTimeSeries, TwelveDataTimeSeriesPoint


def parse_history(symbol: str, payload: object, *, interval: str, start: datetime,
                  end: datetime, outputsize: int) -> TwelveDataTimeSeries:
    if not isinstance(payload, Mapping):
        raise ValueError("Malformed Yahoo history response")
    chart = payload.get("chart")
    if not isinstance(chart, Mapping) or chart.get("error"):
        raise ValueError("Yahoo history lookup failed")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
        raise ValueError("Missing Yahoo history result")
    result = results[0]
    meta = result.get("meta", {})
    if not isinstance(meta, Mapping) or str(meta.get("symbol", "")).upper() != symbol:
        raise ValueError("Yahoo history returned a different symbol")
    zone = ZoneInfo(str(meta.get("exchangeTimezoneName", "UTC")))
    stamps = result.get("timestamp")
    indicators = result.get("indicators", {})
    quotes = indicators.get("quote") if isinstance(indicators, Mapping) else None
    if not isinstance(stamps, list) or len(stamps) > 60000 or not isinstance(quotes, list) or len(quotes) != 1:
        raise ValueError("Missing or oversized Yahoo candle series")
    values = quotes[0]
    if not isinstance(values, Mapping):
        raise ValueError("Malformed Yahoo candle values")
    arrays = {key: values.get(key) for key in ("open", "high", "low", "close")}
    if any(not isinstance(items, list) or len(items) != len(stamps) for items in arrays.values()):
        raise ValueError("Misaligned Yahoo candle arrays")
    rows = {}
    for index, stamp in enumerate(stamps):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise ValueError("Invalid Yahoo candle timestamp")
        day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone)
        # Daily request boundaries refer to exchange-local trading dates.
        in_range = start.date() <= day.date() < end.date() if interval == "1day" else start <= day < end
        if not in_range:
            continue
        raw = {key: items[index] for key, items in arrays.items()}
        if any(value is None for value in raw.values()):
            continue
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in raw.values()):
            raise ValueError("Invalid Yahoo OHLC price")
        if raw["low"] > min(raw["open"], raw["close"]) or raw["high"] < max(raw["open"], raw["close"]):
            raise ValueError("Inconsistent Yahoo OHLC prices")
        date = day.date().isoformat() if interval == "1day" else day.isoformat()
        rows[date] = TwelveDataTimeSeriesPoint(datetime=date, **raw)
    ordered = sorted(rows.values(), key=lambda point: point.datetime, reverse=True)
    if not ordered:
        raise ValueError("No Yahoo candles in the requested range")
    return TwelveDataTimeSeries(symbol=symbol, name=None, exchange=meta.get("exchangeName"),
        instrument_type=meta.get("instrumentType"), currency=meta.get("currency"), interval=interval,
        values=ordered[:outputsize], provider="Yahoo Finance",
        adjustment="provider OHLC, not dividend-adjusted; split basis follows Yahoo history")
