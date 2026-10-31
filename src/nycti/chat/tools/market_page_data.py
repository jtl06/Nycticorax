"""Parse public server-rendered market pages without executing site JavaScript."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from nycti.chat.tools.market_pages import market_page_info


class MarketPageDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QuotePoint:
    price: float
    timestamp: str
    session: str
    basis: str
    reference_price: float
    percent_change: float


@dataclass(frozen=True, slots=True)
class HistoryRow:
    date: str
    high: float
    low: float


@dataclass(frozen=True, slots=True)
class MarketPageObservation:
    url: str
    symbol: str
    observed_at: str
    currency: str | None = None
    regular: QuotePoint | None = None
    extended: QuotePoint | None = None
    reported_ath: float | None = None
    reported_ath_date: str | None = None
    adjustment_basis: str | None = None
    latest_close: float | None = None
    latest_close_date: str | None = None
    history_rows: tuple[HistoryRow, ...] = ()

    def render(self) -> str:
        data = {key: value for key, value in asdict(self).items() if value not in (None, ())}
        return "Direct market page for: " + self.url + "\n" + json.dumps(data, separators=(",", ":"))


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.quote_text: list[str] = []
        self.symbol_data: list[dict] = []
        self.rows: list[list[str]] = []
        self._ignored = 0
        self._quote = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored += 1
        if tag == "h1":
            self._quote = True
        if tag == "nav" and self._quote:
            self._quote = False
        if tag == "tr":
            self._row = []
        if tag in {"td", "th"}:
            self._cell = []
        for key, value in attrs:
            if key == "data-symboldetail" and value:
                try:
                    data = json.loads(value)
                except ValueError as exc:
                    raise MarketPageDataError("Malformed embedded stock data") from exc
                if isinstance(data, dict):
                    self.symbol_data.append(data)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored = max(0, self._ignored - 1)
        if tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join(self._cell))
            self._cell = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self._ignored:
            return
        self.text.append(text)
        if self._quote:
            self.quote_text.append(text)
        if self._cell is not None:
            self._cell.append(text)


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise MarketPageDataError("Invalid price")
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise MarketPageDataError("Invalid price") from exc
    if not math.isfinite(result) or result <= 0:
        raise MarketPageDataError("Nonpositive or nonfinite price")
    return result


def _date(value: str) -> str:
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    raise MarketPageDataError("Unrecognized source date")


def _timestamp(value: str) -> datetime:
    match = re.fullmatch(r"([A-Za-z]{3} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M) (EST|EDT|ET)", value)
    if not match:
        raise MarketPageDataError("Unrecognized quote timestamp")
    local = datetime.strptime(match[1], "%b %d, %Y, %I:%M %p").replace(tzinfo=ZoneInfo("America/New_York"))
    if match[2] != "ET" and local.tzname() != match[2]:
        raise MarketPageDataError("Quote timezone disagrees with source date")
    return local.astimezone(timezone.utc)


def parse_market_page(url: str, html: str, *, now: datetime | None = None) -> MarketPageObservation:
    now = now or datetime.now(timezone.utc)
    if market_page_info(url) is None:
        raise MarketPageDataError("Unsupported market source")
    parsed = urlsplit(url)
    symbol = parsed.path.strip("/").split("/")[1].upper()
    page = _Page()
    page.feed(html)
    if parsed.hostname == "stockscan.io":
        return _stockscan(url, symbol, page, now)
    if parsed.hostname == "stockanalysis.com":
        return _stockanalysis(url, symbol, page, now)
    raise MarketPageDataError("Unsupported market source")


def _stockscan(url, symbol, page, now):
    matching = [item for item in page.symbol_data if item.get("symbol") == symbol]
    if len(matching) != 1:
        raise MarketPageDataError("Missing or ambiguous stock identity")
    text = " ".join(page.text)
    closing = re.search(r"latest closing stock price as of ([A-Za-z]+ \d{1,2}, \d{4}), is \$([\d,.]+)", text)
    ath = re.search(r"all-time high stock price is \$([\d,.]+)\s*, occurred on ([A-Za-z]+ \d{1,2}, \d{4})", text)
    if not closing or not ath:
        raise MarketPageDataError("Missing dated historical facts")
    close_date, ath_date = _date(closing[1]), _date(ath[2])
    close, high = _number(closing[2]), _number(ath[1])
    if ath_date > close_date or close_date > now.date().isoformat() or high < close:
        raise MarketPageDataError("Inconsistent history dates or peak")
    reported = matching[0].get("priceHistory")
    peaks = [item for item in reported if isinstance(item, dict) and item.get("name") == "highest"] if isinstance(reported, list) else []
    if len(peaks) != 1 or _number(peaks[0].get("value")) != high or _date(str(peaks[0].get("date"))) != ath_date:
        raise MarketPageDataError("Visible and embedded ATH disagree")
    rows = []
    for row in page.rows:
        if len(row) != 6:
            continue
        try:
            day, row_high, low = _date(row[0]), _number(row[1]), _number(row[2])
        except MarketPageDataError:
            continue
        if low > row_high or day > close_date:
            raise MarketPageDataError("Inconsistent history row")
        rows.append(HistoryRow(day, row_high, low))
        if len(rows) == 10:
            break
    basis = "split/dividend adjusted" if "adjusted for splits and dividends" in text else "not specified"
    return MarketPageObservation(url, symbol, now.isoformat(), reported_ath=high, reported_ath_date=ath_date,
        adjustment_basis=basis, latest_close=close, latest_close_date=close_date, history_rows=tuple(rows))


def _stockanalysis(url, symbol, page, now):
    tokens = page.quote_text
    identity = next((re.fullmatch(r"[A-Z0-9 ]+: ([A-Z0-9.-]+) [·] (.+) [·] ([A-Z]{3})", token) for token in tokens
                     if re.fullmatch(r"[A-Z0-9 ]+: ([A-Z0-9.-]+) [·] (.+) [·] ([A-Z]{3})", token)), None)
    if identity is None or identity[1] != symbol:
        raise MarketPageDataError("Missing or wrong quote identity")
    previous = next((_number(row[1]) for row in page.rows if len(row) == 2 and row[0] == "Previous Close"), None)
    if previous is None:
        raise MarketPageDataError("Missing regular-session comparison price")
    points = []
    for index, token in enumerate(tokens[:-2]):
        if not re.fullmatch(r"[\d,]+[.]\d+", token):
            continue
        change = re.fullmatch(r"[+−-]?[\d,.]+ [(]([+−-]?[\d.]+)%[)]", tokens[index + 1])
        if not change:
            continue
        tail = tokens[index + 2:index + 5]
        label = tail[0].lower().rstrip(":")
        session = {"at close": "regular close", "as of": "regular", "pre-market": "pre-market", "after-hours": "after-hours"}.get(label)
        if session is None or len(tail) < 2:
            raise MarketPageDataError("Unrecognized quote session")
        stamp = _timestamp(tail[1])
        if stamp > now + timedelta(minutes=5):
            raise MarketPageDataError("Future quote timestamp")
        price = _number(token)
        if not points:
            if session not in {"regular close", "regular"}:
                raise MarketPageDataError("Missing regular price before extended quote")
            base, basis = previous, "vs previous close"
        else:
            if len(points) != 1 or session not in {"pre-market", "after-hours"} or points[0].session != "regular close":
                raise MarketPageDataError("Ambiguous extended-session basis")
            if stamp < datetime.fromisoformat(points[0].timestamp):
                raise MarketPageDataError("Extended quote predates regular close")
            base, basis = points[0].price, "vs regular close"
        percent = (price / base - 1) * 100
        if abs(percent - float(change[1].replace("−", "-"))) > 0.08:
            raise MarketPageDataError("Displayed change disagrees with same-page price basis")
        points.append(QuotePoint(price, stamp.isoformat(), session, basis, base, round(percent, 4)))
    if not points:
        raise MarketPageDataError("No dated quote blocks")
    return MarketPageObservation(url, symbol, now.isoformat(), currency=identity[3], regular=points[0],
                                 extended=points[1] if len(points) > 1 else None)
