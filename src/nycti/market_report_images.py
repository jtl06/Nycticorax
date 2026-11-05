"""Deterministic watchlist heatmaps from delivered, typed report receipts."""
from dataclasses import fields
from functools import lru_cache
from io import BytesIO
import json
import math
from pathlib import Path
import random

from PIL import Image, ImageDraw

from nycti.market_reports import DISPLAY_SESSIONS, MarketReport, QuoteRow
from nycti.table_images import TableImage, TableImageExtraction, _load_font

IMAGE_PREFIX = "nycti-watchlist-"
COLOR_LIMIT_PERCENT = 15.0
STICKER_SIZE = 300
STICKER_OPACITY = 0.40
STICKER_ANGLES = tuple(range(-22, -7)) + tuple(range(8, 23))
ROWS_PER_IMAGE = 20
MAX_IMAGES = 4
WIDTH = 720
ROW_HEIGHT = 64
TOP = 104
BACKGROUND = (24, 24, 24)
NEUTRAL = (35, 37, 40)
RED = (155, 42, 53)
GREEN = (25, 119, 76)
TEXT = (244, 245, 247)
MUTED = (168, 175, 185)
COLUMNS = (20, 130, 410, 700)


def extreme_move_sticker(percent: float | None) -> Image.Image | None:
    if percent is None or not math.isfinite(percent) or abs(percent) <= 25:
        return None
    art = _sticker_art(percent > 0)
    tilted = art.rotate(random.choice(STICKER_ANGLES), resample=Image.Resampling.BICUBIC, expand=True)
    tilted.thumbnail((STICKER_SIZE, STICKER_SIZE), Image.Resampling.LANCZOS)
    tilted.putalpha(tilted.getchannel("A").point(lambda value: round(value * STICKER_OPACITY)))
    sticker = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE))
    sticker.alpha_composite(tilted, ((STICKER_SIZE - tilted.width) // 2, (STICKER_SIZE - tilted.height) // 2))
    return sticker


@lru_cache(maxsize=2)
def _sticker_art(positive: bool) -> Image.Image:
    # Cache only the two bounded base assets, not every random orientation.
    filename = "gigachad.png" if positive else "nuclear-cloud.png"
    with Image.open(Path(__file__).with_name("assets") / filename) as source:
        art = source.convert("RGBA")
    art = art.crop(art.getbbox())
    art.thumbnail((STICKER_SIZE, STICKER_SIZE), Image.Resampling.LANCZOS)
    return art


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
        height = TOP + len(rows) * ROW_HEIGHT + 16
        image = Image.new("RGB", (WIDTH, height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        title = "Watchlist" if len(report.rows) <= ROWS_PER_IMAGE else f"Watchlist ({offset + 1}-{offset + len(rows)} of {len(report.rows)})"
        _draw_text(draw, title, 24, 18, WIDTH - 48, size=28, bold=True)
        subtitle = "Regular vs prev close  |  Extended vs regular close"
        _draw_text(draw, subtitle, 24, 55, WIDTH - 48, size=16, color=MUTED)
        for label, left, right in zip(("Ticker", "Last trading day", "AH / Overnight / Pre"), COLUMNS, COLUMNS[1:]):
            _draw_text(draw, label, left, 82, right - left - 18, size=16, color=MUTED, bold=True)
        stickers = []
        for row_index, row in enumerate(rows):
            y = TOP + row_index * ROW_HEIGHT
            _draw_text(draw, row.symbol, COLUMNS[0] + 4, y + 19, 100, bold=True)
            regular_price, regular_percent = row.regular_price, row.regular_percent
            extended_price, extended_percent = row.extended_price, row.extended_percent
            extended_session = row.extended_session
            # Older receipts have only the selected session; never invent the other return.
            if regular_price is None and row.session in {"regular", "last regular"}:
                regular_price, regular_percent = row.price, row.percent
            if extended_price is None and row.session in {"premarket", "after-hours", "overnight"}:
                extended_price, extended_percent, extended_session = row.price, row.percent, row.session
            for column, price, percent, session in (
                (1, regular_price, regular_percent, "regular" if row.session == "regular" else "close"),
                (2, extended_price, extended_percent, DISPLAY_SESSIONS.get(extended_session, extended_session)),
            ):
                left, right = COLUMNS[column], COLUMNS[column + 1]
                draw.rectangle((left, y, right - 4, y + ROW_HEIGHT - 3), fill=change_color(percent if price is not None else None))
                _draw_text(draw, _money(price, row.currency), left + 10, y + 6, 148, size=23)
                change = f"{percent:+.2f}%" if price is not None and percent is not None else "N/A"
                _draw_text(draw, change, right - 110, y + 8, 98, size=21, bold=True)
                label = "vs prev close" if column == 1 else (
                    f"{session.replace(' market', '')} vs close {_money(regular_price, row.currency)}" if price is not None else "unavailable")
                _draw_text(draw, label, left + 10, y + 37, right - left - 22, size=14, color=MUTED)
            moves = [value for value in (regular_percent, extended_percent) if value is not None and math.isfinite(value)]
            move = max(moves, key=abs) if moves else None
            sticker = extreme_move_sticker(move)
            if sticker is not None:
                size = min(STICKER_SIZE, height - 24)
                sticker = sticker.resize((size, size), Image.Resampling.LANCZOS)
                stickers.append((sticker, (WIDTH - size - 20, max(8, min(y + ROW_HEIGHT // 2 - size // 2, height - size - 8)))))
        # Deliberately overlap neighboring rows; low opacity keeps values visible.
        for sticker, position in stickers:
            image.paste(sticker, position, sticker)
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
            if key in {"price", "percent", "reference_price", "regular_price", "regular_percent", "extended_price", "extended_percent"}:
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
