from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from nycti.twelvedata.models import (
    TwelveDataDataError,
    TwelveDataPriceExtrema,
    TwelveDataTimeSeries,
    TwelveDataTimeSeriesPoint,
)

MAX_TIME_SERIES_POINTS = 5000
MAX_EXTREMA_PAGES = 4


async def process_price_extrema(
    fetch_page: Callable[[str | None], Awaitable[TwelveDataTimeSeries]],
    *,
    start_date: str | None,
    end_date: str | None,
    page_size: int = MAX_TIME_SERIES_POINTS,
    max_pages: int = MAX_EXTREMA_PAGES,
) -> TwelveDataPriceExtrema:
    """Page through daily history and reduce it to compact exact extrema."""
    pages: list[TwelveDataTimeSeries] = []
    next_end = end_date
    requested_start = _parse_date(start_date)
    seen_oldest_dates: set[date] = set()
    coverage_complete = False

    for _ in range(max(max_pages, 1)):
        page = await fetch_page(next_end)
        pages.append(page)
        if not page.values:
            coverage_complete = True
            break

        oldest = min(_point_date(point) for point in page.values)
        if oldest in seen_oldest_dates:
            break
        seen_oldest_dates.add(oldest)
        if requested_start is not None and oldest <= requested_start:
            coverage_complete = True
            break
        if len(page.values) < page_size:
            coverage_complete = True
            break
        next_end = (oldest - timedelta(days=1)).isoformat()

    return summarize_price_extrema(pages, coverage_complete=coverage_complete)


def summarize_price_extrema(
    pages: list[TwelveDataTimeSeries],
    *,
    coverage_complete: bool,
) -> TwelveDataPriceExtrema:
    if not pages:
        raise TwelveDataDataError("Price extrema processing received no history pages.")
    first_page = pages[0]
    points_by_datetime = {
        point.datetime: point
        for page in pages
        for point in page.values
    }
    points = sorted(points_by_datetime.values(), key=_point_date, reverse=True)
    if not points:
        raise TwelveDataDataError("Price extrema processing received no historical values.")
    points_with_high = [point for point in points if point.high is not None]
    points_with_low = [point for point in points if point.low is not None]
    points_with_close = [point for point in points if point.close is not None]
    if not points_with_high or not points_with_low or not points_with_close:
        raise TwelveDataDataError("Price extrema processing requires high, low, and close values.")

    return TwelveDataPriceExtrema(
        symbol=first_page.symbol,
        name=first_page.name,
        exchange=first_page.exchange,
        instrument_type=first_page.instrument_type,
        currency=first_page.currency,
        coverage_start=points[-1].datetime,
        coverage_end=points[0].datetime,
        candle_count=len(points),
        highest_intraday=max(points_with_high, key=lambda point: float(point.high)),
        highest_close=max(points_with_close, key=lambda point: float(point.close)),
        lowest_intraday=min(points_with_low, key=lambda point: float(point.low)),
        latest=max(points_with_close, key=_point_date),
        provider_request_count=len(pages),
        coverage_complete=coverage_complete,
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise TwelveDataDataError(f"Invalid historical date: {value}") from exc


def _point_date(point: TwelveDataTimeSeriesPoint) -> date:
    parsed = _parse_date(point.datetime)
    if parsed is None:  # pragma: no cover - model requires datetime
        raise TwelveDataDataError("Historical point did not include a date.")
    return parsed
