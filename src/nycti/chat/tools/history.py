"""Provider recovery for history, shared by single-symbol and batch tools."""
from nycti.chat.tools.market_page_client import render_market_observation
from nycti.chat.tools.market_pages import STOCKSCAN, market_page_url
from nycti.twelvedata.formatting import format_price_extrema_message, format_price_history_message
from nycti.twelvedata.models import TwelveDataAPIKeyMissingError, TwelveDataHTTPError, TwelveDataDataError
from nycti.twelvedata.processing import process_price_extrema, summarize_price_extrema
from nycti.yahoo import YahooFinanceDataError, YahooFinanceHTTPError

HISTORY_SUCCESS_PREFIXES = ("Twelve Data price history for:", "Yahoo Finance price history for:",
                            "Direct market page for:")


async def execute_history(executor, *, symbol, mode, interval, outputsize, start_date, end_date):
    primary_error = "Primary history provider unavailable"
    try:
        if mode == "extrema":
            async def fetch_page(page_end):
                return await executor.market_data_client.get_price_history(
                    symbol, interval="1day", outputsize=5000, start_date=start_date, end_date=page_end)
            summary = await process_price_extrema(fetch_page, start_date=start_date, end_date=end_date)
            return format_price_extrema_message(summary)
        series = await executor.market_data_client.get_price_history(
            symbol, interval=interval, outputsize=outputsize, start_date=start_date, end_date=end_date)
        if not series.values:
            raise TwelveDataDataError("No primary history candles in the requested range")
        return format_price_history_message(series)
    except (TwelveDataAPIKeyMissingError, TwelveDataHTTPError, TwelveDataDataError) as exc:
        primary_error = str(exc)[:240]

    yahoo = getattr(executor, "yahoo_finance_client", None)
    fallback = getattr(yahoo, "get_price_history", None)
    fallback_error = "Yahoo history is not configured"
    if callable(fallback):
        try:
            series = await fallback(symbol, interval="1day" if mode == "extrema" else interval,
                outputsize=60000 if mode == "extrema" else outputsize, start_date=start_date,
                end_date=end_date, full_history=mode == "extrema")
            if not series.values:
                raise YahooFinanceDataError("No alternate history candles in the requested range")
            if mode == "extrema":
                # A returned series cannot prove complete listing coverage. Do not
                # silently promote its range maximum to a verified all-time high.
                summary = summarize_price_extrema([series], coverage_complete=False)
                result = format_price_extrema_message(summary)
            else:
                result = format_price_history_message(series)
            return result + "\nRecovery: primary provider unavailable; used Yahoo history."
        except (YahooFinanceHTTPError, YahooFinanceDataError, TwelveDataDataError, OSError) as exc:
            fallback_error = str(exc)[:240]

    # StockScan reports a peak, not historical closes. Never use it to invent
    # week-start closes or satisfy a bounded date-range extrema request.
    page_client = getattr(executor, "market_page_client", None)
    url = market_page_url("stockscan", symbol)
    if mode == "extrema" and not start_date and not end_date and page_client is not None and url:
        try:
            observation = await page_client.get(url)
            if observation.reported_ath is None:
                raise ValueError("No reported peak on the alternate page")
            return render_market_observation(observation, STOCKSCAN.notes)
        except (OSError, ValueError, TimeoutError):
            pass
    return f"Price history for `{symbol.upper()}` unavailable. Primary: {primary_error}. Alternate: {fallback_error}."
