from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nycti.yahoo.models import YahooAnnualPerformance, YahooAnnualPerformanceYear


def parse_annual_performance(
    requested_symbol: str,
    payload: Mapping[str, object],
    *,
    start_year: int,
    now: datetime,
) -> YahooAnnualPerformance:
    result = _chart_result(payload)
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        raise ValueError("Yahoo Finance chart result did not include metadata.")
    timezone_name = str(meta.get("exchangeTimezoneName", "")).strip() or None
    exchange_timezone = _timezone(timezone_name)
    points = _daily_closes(result, exchange_timezone)
    if not points:
        raise ValueError("Yahoo Finance chart result did not include daily closes.")
    distributions = _annual_distributions(result, exchange_timezone)
    local_now = now.astimezone(exchange_timezone)
    years: list[YahooAnnualPerformanceYear] = []
    for year in range(start_year, local_now.year + 1):
        year_points = [(date, close) for date, close in points if date.year == year]
        if not year_points:
            continue
        prior_points = [(date, close) for date, close in points if date.year < year]
        start_date, start_price = prior_points[-1] if prior_points else year_points[0]
        end_date, end_price = year_points[-1]
        years.append(
            YahooAnnualPerformanceYear(
                year=year,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                start_price=start_price,
                end_price=end_price,
                price_change_percent=(end_price / start_price - 1) * 100,
                distributions_per_share=distributions.get(year, 0.0),
                distribution_percent_of_start=distributions.get(year, 0.0) / start_price * 100,
                partial_year=not prior_points or year == local_now.year,
            )
        )
    if not years:
        raise ValueError("Yahoo Finance returned no annual performance rows.")
    symbol = str(meta.get("symbol", "")).strip() or requested_symbol
    return YahooAnnualPerformance(
        requested_symbol=requested_symbol,
        symbol=symbol,
        currency=str(meta.get("currency", "")).strip() or None,
        timezone_name=timezone_name,
        years=tuple(years),
        source_url=f"https://finance.yahoo.com/quote/{symbol}/history/",
    )


def format_annual_performance(performance: YahooAnnualPerformance) -> str:
    alias = (
        f" (Yahoo symbol {performance.symbol})"
        if performance.symbol != performance.requested_symbol
        else ""
    )
    lines = [
        f"Yahoo Finance annual performance for {performance.requested_symbol}{alias}",
        "Method: price change uses prior calendar-year final close to each year-end close; "
        "cash distribution percent is distributions per share divided by that starting close.",
    ]
    for row in performance.years:
        partial = " partial/YTD" if row.partial_year else ""
        lines.append(
            f"{row.year}{partial}: price {row.price_change_percent:+.2f}% "
            f"({row.start_price:.4f} on {row.start_date} -> {row.end_price:.4f} on {row.end_date}); "
            f"cash distributions {row.distributions_per_share:.4f} "
            f"({row.distribution_percent_of_start:.2f}% of start price)"
        )
    lines.append(f"Source: {performance.source_url}")
    return "\n".join(lines)


def _chart_result(payload: Mapping[str, object]) -> Mapping[str, object]:
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise ValueError("Yahoo Finance response did not include chart data.")
    if chart.get("error"):
        raise ValueError(str(chart["error"]))
    results = chart.get("result")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
        raise ValueError("Yahoo Finance chart response did not include a result.")
    result = results[0]
    if not isinstance(result, Mapping):
        raise ValueError("Yahoo Finance chart result had an unexpected shape.")
    return result


def _daily_closes(
    result: Mapping[str, object],
    exchange_timezone: ZoneInfo,
) -> list[tuple[date, float]]:
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, Sequence) or isinstance(timestamps, (str, bytes)):
        return []
    if not isinstance(indicators, Mapping):
        return []
    quotes = indicators.get("quote")
    if not isinstance(quotes, Sequence) or isinstance(quotes, (str, bytes)) or not quotes:
        return []
    quote = quotes[0]
    closes = quote.get("close") if isinstance(quote, Mapping) else None
    if not isinstance(closes, Sequence) or isinstance(closes, (str, bytes)):
        return []
    points: list[tuple[date, float]] = []
    for raw_timestamp, raw_close in zip(timestamps, closes):
        if not isinstance(raw_timestamp, (int, float)) or not isinstance(raw_close, (int, float)):
            continue
        date = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc).astimezone(exchange_timezone).date()
        points.append((date, float(raw_close)))
    return sorted(points)


def _annual_distributions(
    result: Mapping[str, object],
    exchange_timezone: ZoneInfo,
) -> dict[int, float]:
    events = result.get("events")
    dividends = events.get("dividends") if isinstance(events, Mapping) else None
    if not isinstance(dividends, Mapping):
        return {}
    totals: dict[int, float] = {}
    for event in dividends.values():
        if not isinstance(event, Mapping):
            continue
        timestamp = event.get("date")
        amount = event.get("amount")
        if not isinstance(timestamp, (int, float)) or not isinstance(amount, (int, float)):
            continue
        year = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(exchange_timezone).year
        totals[year] = totals.get(year, 0.0) + float(amount)
    return totals


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")
