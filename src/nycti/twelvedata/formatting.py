from __future__ import annotations

from nycti.twelvedata.models import (
    TwelveDataPriceExtrema,
    TwelveDataQuote,
    TwelveDataSymbolMatch,
    TwelveDataTimeSeries,
)


def format_market_quote_message(
    quote: TwelveDataQuote,
    *,
    include_price_details: bool = True,
) -> str:
    header = quote.name or quote.symbol
    lines = [f"Twelve Data market quote for: {header} ({quote.symbol})"]
    if quote.name and quote.exchange:
        lines.append(
            "Current provider identity: "
            f"{quote.symbol} resolves to {quote.name} on {quote.exchange}; "
            "prefer this dated listing metadata over older symbol information."
        )
    if quote.instrument_type or quote.exchange:
        detail_parts = [part for part in (quote.instrument_type, quote.exchange) if part]
        lines.append("Instrument: " + " | ".join(detail_parts))
    if not include_price_details:
        return "\n".join(lines)
    if quote.close is not None:
        currency_prefix = f"{quote.currency} " if quote.currency else ""
        lines.append(f"Last price: {currency_prefix}{quote.close:.4f}")
    else:
        lines.append("No latest price was available.")
        return "\n".join(lines)
    if quote.datetime:
        lines.append(f"Quote time: {quote.datetime}")
    if quote.change is not None:
        change_parts = [_format_signed_price(quote.change)]
        if quote.percent_change is not None:
            change_parts.append(f"({quote.percent_change:+.2f}%)")
        if quote.previous_close is not None:
            change_parts.append(f"vs prev close {quote.previous_close:.4f}")
        lines.append("Change: " + " ".join(change_parts))
    if quote.open is not None and quote.high is not None and quote.low is not None:
        lines.append(f"Day range: open {quote.open:.4f} | high {quote.high:.4f} | low {quote.low:.4f}")
    if quote.volume is not None:
        lines.append(f"Volume: {quote.volume:,}")
    if quote.is_market_open is not None:
        lines.append(f"Market open: {'yes' if quote.is_market_open else 'no'}")
    return "\n".join(lines)


def format_symbol_suggestions_message(symbol: str, matches: list[TwelveDataSymbolMatch]) -> str:
    lines = [
        f"Twelve Data could not quote `{symbol}` directly. It may need a different exchange-specific or provider-specific symbol."
    ]
    if not matches:
        return "\n".join(lines)
    lines.append("Closest matches:")
    for match in matches[:3]:
        detail_parts = [part for part in (match.instrument_name, match.instrument_type, match.exchange, match.country) if part]
        details = " | ".join(detail_parts)
        lines.append(f"- `{match.symbol}`" + (f": {details}" if details else ""))
    return "\n".join(lines)


def format_price_history_message(series: TwelveDataTimeSeries) -> str:
    header = series.name or series.symbol
    lines = [f"Twelve Data price history for: {header} ({series.symbol})"]
    detail_parts = [part for part in (series.interval, series.instrument_type, series.exchange) if part]
    if detail_parts:
        lines.append("Series: " + " | ".join(detail_parts))
    if series.currency:
        lines.append(f"Currency: {series.currency}")
    if not series.values:
        lines.append("No historical price data was available.")
        return "\n".join(lines)
    lines.append(f"Returned candles: {len(series.values)}")
    lines.append(f"Time range: {series.values[-1].datetime} -> {series.values[0].datetime}")
    lines.append("Recent candles:")
    for point in series.values[: min(len(series.values), 6)]:
        parts = [f"close {point.close:.4f}" if point.close is not None else "close n/a"]
        if point.open is not None:
            parts.append(f"open {point.open:.4f}")
        if point.high is not None:
            parts.append(f"high {point.high:.4f}")
        if point.low is not None:
            parts.append(f"low {point.low:.4f}")
        if point.volume is not None:
            parts.append(f"volume {point.volume:,}")
        lines.append(f"- {point.datetime}: " + " | ".join(parts))
    return "\n".join(lines)


def format_price_extrema_message(summary: TwelveDataPriceExtrema) -> str:
    header = summary.name or summary.symbol
    currency_prefix = f"{summary.currency} " if summary.currency else ""
    lines = [f"Twelve Data price history for: {header} ({summary.symbol})"]
    detail_parts = [part for part in (summary.instrument_type, summary.exchange) if part]
    lines.append("Mode: compact extrema | daily | split-adjusted")
    if detail_parts:
        lines.append("Instrument: " + " | ".join(detail_parts))
    lines.extend(
        [
            f"Coverage: {summary.coverage_start} -> {summary.coverage_end}",
            f"Daily candles processed: {summary.candle_count:,}",
            "Full requested/provider range scanned: " + ("yes" if summary.coverage_complete else "no"),
            f"Highest intraday: {currency_prefix}{summary.highest_intraday.high:.4f} on "
            f"{summary.highest_intraday.datetime}",
            f"Highest daily close: {currency_prefix}{summary.highest_close.close:.4f} on "
            f"{summary.highest_close.datetime}",
            f"Lowest intraday: {currency_prefix}{summary.lowest_intraday.low:.4f} on "
            f"{summary.lowest_intraday.datetime}",
            f"Latest history close: {currency_prefix}{summary.latest.close:.4f} on {summary.latest.datetime}",
            f"Processor: {summary.provider_request_count} provider request(s); raw candles were not sent to the model.",
        ]
    )
    if not summary.coverage_complete:
        lines.append("Coverage warning: extrema are exact only for the displayed range; do not call them all-time.")
    return "\n".join(lines)


def _format_signed_price(value: float) -> str:
    return f"{value:+.4f}"
