"""Supported public market-page routes and evidence interpretation, not quote feeds."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MarketPage:
    focus: str
    notes: str


STOCKSCAN = MarketPage(
    focus="reported all-time high price and date; adjustment basis for splits/dividends; history coverage; "
          "dated historical prices; distinguish intraday highs from closing highs",
    notes="StockScan page evidence, not a complete candle series. Preserve the stated split/dividend adjustment "
          "basis and coverage. Attribute a page-reported ATH; do not claim a verified full-listing-history maximum "
          "or classify it as intraday/closing unless supported. Do not mix adjusted and unadjusted prices.",
)
STOCKANALYSIS = MarketPage(
    focus="ticker and exchange; currency; regular-session price and timestamp; premarket or after-hours price "
          "and timestamp; regular close and previous close; percentage-change comparison basis; delay label",
    notes="StockAnalysis page evidence, not a streaming feed. Check each price's own timestamp, currency, "
          "session, delay and comparison basis. Do not infer freshness from the site's real-time label or "
          "describe premarket/after-hours as overnight. Keep Yahoo for overnight quotes.",
)


def market_page_url(source: str, symbol: str) -> str | None:
    """Only simple US stock routes; other listings can use an exact public URL."""
    ticker = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", ticker):
        return None
    if source == "stockscan":
        return f"https://stockscan.io/stocks/{ticker}/price-history"
    if source == "stockanalysis":
        return f"https://stockanalysis.com/stocks/{ticker.lower()}/"
    return None


def market_page_info(url: str) -> MarketPage | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return None
    if parsed.hostname == "stockscan.io" and re.fullmatch(r"/stocks/[A-Za-z0-9.-]+/price-history/?", parsed.path):
        return STOCKSCAN
    if parsed.hostname == "stockanalysis.com" and re.fullmatch(r"/(?:stocks|etf)/[a-z0-9.-]+/", parsed.path):
        return STOCKANALYSIS
    return None
