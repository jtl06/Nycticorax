from __future__ import annotations

from nycti.twelvedata.models import TwelveDataQuote, TwelveDataSymbolMatch


def format_market_quote_message(quote: TwelveDataQuote) -> str:
    header = quote.name or quote.symbol
    lines = [f"Twelve Data market quote for: {header} ({quote.symbol})"]
    if quote.instrument_type or quote.exchange:
        detail_parts = [part for part in (quote.instrument_type, quote.exchange) if part]
        lines.append("Instrument: " + " | ".join(detail_parts))
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


def _format_signed_price(value: float) -> str:
    return f"{value:+.4f}"
