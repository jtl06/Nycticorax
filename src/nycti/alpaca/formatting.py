from __future__ import annotations

from nycti.alpaca.models import AlpacaStockSnapshot


def format_stock_snapshot_message(snapshot: AlpacaStockSnapshot) -> str:
    lines = [f"Alpaca stock snapshot for: {snapshot.symbol}", f"Feed: {snapshot.feed}"]
    latest_trade = snapshot.latest_trade
    if latest_trade is None:
        lines.append("No latest trade was available.")
        return "\n".join(lines)

    lines.append(f"Last trade: ${latest_trade.price:.4f}")
    lines.append(f"Trade time: {latest_trade.timestamp}")

    if snapshot.latest_quote is not None:
        quote = snapshot.latest_quote
        spread = max(quote.ask_price - quote.bid_price, 0.0)
        lines.append(
            f"Bid/ask: ${quote.bid_price:.4f} / ${quote.ask_price:.4f} (spread ${spread:.4f})"
        )

    previous_close = snapshot.previous_daily_bar.close if snapshot.previous_daily_bar is not None else None
    if previous_close not in (None, 0):
        delta = latest_trade.price - previous_close
        percent = (delta / previous_close) * 100
        lines.append(
            f"Change vs prev close (${previous_close:.4f}): {_format_signed_price(delta)} ({percent:+.2f}%)"
        )

    if snapshot.daily_bar is not None:
        daily_close = snapshot.daily_bar.close
        lines.append(f"Latest daily close: ${daily_close:.4f}")

    return "\n".join(lines)


def _format_signed_price(value: float) -> str:
    return f"{value:+.4f}"
