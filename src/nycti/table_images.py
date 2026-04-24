from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True, slots=True)
class TableImage:
    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class TableImageExtraction:
    text: str
    images: list[TableImage]


MAX_TABLE_IMAGES = 5
MAX_CELL_CHARS = 360
FONT_SIZE = 16
CELL_PADDING_X = 14
CELL_PADDING_Y = 10
LINE_SPACING = 5
MIN_COLUMN_WIDTH = 120
MAX_COLUMN_WIDTH = 440
MAX_IMAGE_WIDTH = 1500
HEADER_BG = (230, 235, 243)
ROW_BG = (255, 255, 255)
ALT_ROW_BG = (248, 250, 252)
GRID = (200, 206, 215)
TEXT = (22, 27, 34)
HEADER_TEXT = (12, 17, 23)


def extract_markdown_tables_as_images(text: str) -> TableImageExtraction:
    lines = text.splitlines()
    output_lines: list[str] = []
    images: list[TableImage] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            output_lines.append(line)
            index += 1
            continue
        if not in_fence and len(images) < MAX_TABLE_IMAGES and _looks_like_markdown_table_header(lines, index):
            table_lines = [lines[index]]
            index += 2
            while index < len(lines) and _looks_like_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            image_index = len(images) + 1
            image = render_markdown_table_image(table_lines, filename=f"table-{image_index}.png")
            images.append(image)
            output_lines.append(f"[attached table image: {image.filename}]")
            continue
        output_lines.append(line)
        index += 1
    return TableImageExtraction(text="\n".join(output_lines).strip(), images=images)


def render_markdown_table_image(lines: list[str], *, filename: str = "table.png") -> TableImage:
    rows = [
        _split_table_cells(line)
        for line in lines
        if not _looks_like_table_separator(line)
    ]
    column_count = max((len(row) for row in rows), default=0)
    padded_rows = [row + [""] * (column_count - len(row)) for row in rows]
    font = _load_font(size=FONT_SIZE, bold=False)
    bold_font = _load_font(size=FONT_SIZE, bold=True)
    probe = Image.new("RGB", (1, 1), "white")
    draw = ImageDraw.Draw(probe)
    cell_text = [
        [_clean_cell(cell) for cell in row]
        for row in padded_rows
    ]
    if not cell_text or column_count == 0:
        return _render_empty_table(filename=filename)
    widths = _measure_column_widths(draw, cell_text, font=font, bold_font=bold_font)
    wrapped_rows = [
        [
            _wrap_cell_text(
                value,
                max_width=max(widths[column] - (CELL_PADDING_X * 2), 20),
                font=bold_font if row_index == 0 else font,
                draw=draw,
            )
            for column, value in enumerate(row)
        ]
        for row_index, row in enumerate(cell_text)
    ]
    line_height = _line_height(draw, font)
    row_heights = [
        max(
            34,
            max((len(cell_lines) for cell_lines in row), default=1) * line_height
            + (max((len(cell_lines) for cell_lines in row), default=1) - 1) * LINE_SPACING
            + CELL_PADDING_Y * 2,
        )
        for row in wrapped_rows
    ]
    image_width = sum(widths) + 1
    image_height = sum(row_heights) + 1
    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)
    y = 0
    for row_index, row in enumerate(wrapped_rows):
        x = 0
        bg = HEADER_BG if row_index == 0 else (ALT_ROW_BG if row_index % 2 == 0 else ROW_BG)
        current_font = bold_font if row_index == 0 else font
        fill = HEADER_TEXT if row_index == 0 else TEXT
        for column, cell_lines in enumerate(row):
            draw.rectangle((x, y, x + widths[column], y + row_heights[row_index]), fill=bg, outline=GRID)
            text_y = y + CELL_PADDING_Y
            for line in cell_lines:
                draw.text((x + CELL_PADDING_X, text_y), line, fill=fill, font=current_font)
                text_y += line_height + LINE_SPACING
            x += widths[column]
        y += row_heights[row_index]
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return TableImage(filename=filename, data=output.getvalue())


def _render_empty_table(*, filename: str) -> TableImage:
    image = Image.new("RGB", (320, 64), "white")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return TableImage(filename=filename, data=output.getvalue())


def _measure_column_widths(
    draw: ImageDraw.ImageDraw,
    rows: list[list[str]],
    *,
    font: ImageFont.ImageFont,
    bold_font: ImageFont.ImageFont,
) -> list[int]:
    column_count = len(rows[0])
    widths: list[int] = []
    for column in range(column_count):
        natural_width = MIN_COLUMN_WIDTH
        for row_index, row in enumerate(rows):
            current_font = bold_font if row_index == 0 else font
            words = row[column].split()
            longest_word = max(words, key=len) if words else row[column]
            text = row[column]
            if len(text) > 120:
                desired_width = 380
            elif len(text) > 60:
                desired_width = 320
            elif len(text) > 32:
                desired_width = 240
            else:
                desired_width = _text_width(draw, text, current_font) + CELL_PADDING_X * 2
            word_width = _text_width(draw, longest_word, current_font) + CELL_PADDING_X * 2
            natural_width = max(natural_width, desired_width, word_width)
        widths.append(min(natural_width, MAX_COLUMN_WIDTH))
    total_width = sum(widths)
    if total_width <= MAX_IMAGE_WIDTH:
        return widths
    scale = MAX_IMAGE_WIDTH / total_width
    minimum = max(90, min(MIN_COLUMN_WIDTH, MAX_IMAGE_WIDTH // max(column_count, 1)))
    return [max(minimum, int(width * scale)) for width in widths]


def _wrap_cell_text(
    text: str,
    *,
    max_width: int,
    font: ImageFont.ImageFont,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if _text_width(draw, candidate, font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            if _text_width(draw, word, font) <= max_width:
                current = word
            else:
                broken = _break_long_word(word, max_width=max_width, font=font, draw=draw)
                lines.extend(broken[:-1])
                current = broken[-1] if broken else ""
        if current:
            lines.append(current)
    return lines or [""]


def _break_long_word(
    word: str,
    *,
    max_width: int,
    font: ImageFont.ImageFont,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = char
    if current:
        chunks.append(current)
    return chunks


def _load_font(*, size: int, bold: bool) -> ImageFont.ImageFont:
    candidates = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return max(bbox[3] - bbox[1], 12)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _looks_like_markdown_table_header(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if not _looks_like_table_row(header):
        return False
    return _looks_like_table_separator(separator)


def _looks_like_table_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?", line.strip()))


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and not stripped.startswith("```")


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _clean_cell(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"cite[^]+", "", cleaned)
    cleaned = re.sub(r"[\ue000-\uf8ff]+", " ", cleaned)
    cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufffd]+", "", cleaned)
    cleaned = re.sub(r"【[^】]{1,80}】", "", cleaned)
    cleaned = re.sub(r"\[\^[^\]]+\]", "", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= MAX_CELL_CHARS:
        return cleaned
    return textwrap.shorten(cleaned, width=MAX_CELL_CHARS, placeholder="...")
