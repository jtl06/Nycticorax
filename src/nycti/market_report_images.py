"""Deterministic watchlist heatmaps from delivered, typed report receipts."""
from dataclasses import fields
from io import BytesIO
import json
import math

from PIL import Image, ImageDraw

from nycti.market_reports import DISPLAY_SESSIONS, MarketReport, QuoteRow
from nycti.table_images import TableImage, TableImageExtraction, _load_font

IMAGE_PREFIX = "nycti-watchlist-"
COLOR_LIMIT_PERCENT = 15.0
ROWS_PER_IMAGE = 20
MAX_IMAGES = 4
WIDTH = 920
ROW_HEIGHT = 54
TOP = 112
BACKGROUND = (24, 24, 24)
NEUTRAL = (35, 37, 40)
RED = (155, 42, 53)
GREEN = (25, 119, 76)
TEXT = (244, 245, 247)
MUTED = (168, 175, 185)
COLUMNS = (24, 160, 350, 520, 748, 896)


def change_color(percent: float | None) -> tuple[int, int, int]:
    if percent is None or not math.isfinite(percent):
        return NEUTRAL
    intensity = min(abs(percent) / COLOR_LIMIT_PERCENT, 1.0)
    target = GREEN if percent > 0 else RED
    return tuple(round(base + (value - base) * intensity) for base, value in zip(NEUTRAL, target))


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "N/A"
    amount = f"{value:,.2f}" if abs(value) < 1e12 else f"{value:.4g}"
    return f"${amount}" if currency == "USD" else f"{currency} {amount}".strip()


def _draw_text(draw, text, x, y, width, *, bold=False, color=TEXT, size=23):
    # Fit long tickers, currencies and large prices without changing grid tracks.
    font = _load_font(size=size, bold=bold)
    while draw.textbbox((0, 0), text, font=font)[2] > width and size > 6:
        size -= 1
        font = _load_font(size=size, bold=bold)
    draw.text((x, y), text, font=font, fill=color)


def render_market_report_images(report: MarketReport, *, index: int = 1) -> list[TableImage]:
    if not report.rows or len(report.rows) > 40:
        raise ValueError("A watchlist image requires 1-40 rows")
    images = []
    for page, offset in enumerate(range(0, len(report.rows), ROWS_PER_IMAGE), 1):
        rows = report.rows[offset:offset + ROWS_PER_IMAGE]
        height = TOP + len(rows) * ROW_HEIGHT + 64
        image = Image.new("RGB", (WIDTH, height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        title = "Watchlist" if len(report.rows) <= ROWS_PER_IMAGE else f"Watchlist ({offset + 1}-{offset + len(rows)} of {len(report.rows)})"
        _draw_text(draw, title, 24, 18, WIDTH - 48, size=28, bold=True)
        subtitle = {"regular": "Regular-session prices", "pre": "Premarket prices", "post": "AH prices",
                    "overnight": "Overnight prices"}.get(report.requested_session, "Latest available prices")
        _draw_text(draw, subtitle, 24, 55, WIDTH - 48, size=16, color=MUTED)
        for label, left, right in zip(("Ticker", "Price", "Change", "Compared With", "Session"), COLUMNS, COLUMNS[1:]):
            _draw_text(draw, label, left, 86, right - left - 18, size=16, color=MUTED, bold=True)
        for row_index, row in enumerate(rows):
            y = TOP + row_index * ROW_HEIGHT
            draw.rectangle((24, y, WIDTH - 24, y + ROW_HEIGHT - 3), fill=NEUTRAL)
            color = change_color(row.percent if row.price is not None else None)
            draw.rectangle((COLUMNS[2], y, COLUMNS[3] - 4, y + ROW_HEIGHT - 3), fill=color)
            change = f"{row.percent:+.2f}%" if row.price is not None and row.percent is not None else "N/A"
            values = (row.symbol, _money(row.price, row.currency), change)
            for column, value in enumerate(values):
                _draw_text(draw, value, COLUMNS[column] + 8, y + 11,
                           COLUMNS[column + 1] - COLUMNS[column] - 22, bold=column == 0)
            basis = row.basis.removeprefix("vs ").replace("regular close", "close")
            reference = _money(row.reference_price, row.currency) if row.reference_price is not None else "N/A"
            _draw_text(draw, basis or "Unavailable", COLUMNS[3] + 8, y + 4,
                       COLUMNS[4] - COLUMNS[3] - 22, size=16, color=MUTED)
            _draw_text(draw, reference, COLUMNS[3] + 8, y + 24,
                       COLUMNS[4] - COLUMNS[3] - 22, size=18)
            session = DISPLAY_SESSIONS.get(row.session, row.session) if row.price is not None else "N/A"
            session = "overnight" if session == "overnight market" else session
            _draw_text(draw, session, COLUMNS[4] + 8, y + 14, COLUMNS[5] - COLUMNS[4] - 22, size=17)
        footer_y = TOP + len(rows) * ROW_HEIGHT + 12
        _draw_text(draw, "Color intensity caps at +/-15%. Numbers show the actual move. N/A = unavailable.",
                   24, footer_y, WIDTH - 48, size=15, color=MUTED)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        images.append(TableImage(f"{IMAGE_PREFIX}{index}-{page}.png", output.getvalue()))
    return images


def _receipt_report(item: object) -> MarketReport | None:
    if not isinstance(item, dict) or item.get("display") != "image":
        return None
    rows = item.get("rows")
    session = item.get("requested_session")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 40 or not isinstance(session, str) or session not in {"latest", "regular", "pre", "post", "overnight"}:
        return None
    allowed = {field.name for field in fields(QuoteRow)}
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed or not isinstance(row.get("symbol"), str):
            return None
        if not row["symbol"].strip() or len(row["symbol"]) > 20:
            return None
        for key, value in row.items():
            if key in {"price", "percent", "reference_price"}:
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    return None
            elif not isinstance(value, str) or len(value) > (64 if key != "unavailable" else 256):
                return None
        parsed.append(QuoteRow(**row))
    return MarketReport(tuple(parsed), session)


def extract_market_report_images(text: str, receipts_json: str) -> TableImageExtraction:
    """Replace only exact server-rendered report blocks, never model-written numbers."""
    if len(receipts_json) > 100000:
        return TableImageExtraction(text, [])
    try:
        receipts = json.loads(receipts_json)
    except (TypeError, ValueError):
        return TableImageExtraction(text, [])
    if not isinstance(receipts, list):
        return TableImageExtraction(text, [])
    sections = text.split("\n\n")
    images = []
    for item in receipts[-3:]:
        report = _receipt_report(item)
        if report is None or report.render() not in sections:
            continue
        if len(images) + math.ceil(len(report.rows) / ROWS_PER_IMAGE) > MAX_IMAGES:
            continue
        rendered = render_market_report_images(report, index=len(images) + 1)
        sections = [section for section in sections if section != report.render()]
        images.extend(rendered)
    return TableImageExtraction("\n\n".join(sections).strip(), images)
