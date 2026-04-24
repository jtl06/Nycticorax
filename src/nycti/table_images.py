from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re

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
MAX_CELL_CHARS = 80
CELL_PADDING_X = 12
CELL_PADDING_Y = 8
HEADER_BG = (235, 239, 245)
ROW_BG = (255, 255, 255)
ALT_ROW_BG = (248, 250, 252)
GRID = (200, 206, 215)
TEXT = (22, 27, 34)


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
    rows = [_split_table_cells(line) for line in lines]
    column_count = max((len(row) for row in rows), default=0)
    padded_rows = [row + [""] * (column_count - len(row)) for row in rows]
    font = ImageFont.load_default()
    bold_font = ImageFont.load_default()
    probe = Image.new("RGB", (1, 1), "white")
    draw = ImageDraw.Draw(probe)
    cell_text = [
        [_truncate_cell(cell) for cell in row]
        for row in padded_rows
    ]
    widths = []
    for column in range(column_count):
        max_width = 48
        for row in cell_text:
            bbox = draw.textbbox((0, 0), row[column], font=font)
            max_width = max(max_width, bbox[2] - bbox[0] + CELL_PADDING_X * 2)
        widths.append(max_width)
    row_height = max(28, draw.textbbox((0, 0), "Ag", font=font)[3] + CELL_PADDING_Y * 2)
    image_width = sum(widths) + 1
    image_height = row_height * len(cell_text) + 1
    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)
    y = 0
    for row_index, row in enumerate(cell_text):
        x = 0
        bg = HEADER_BG if row_index == 0 else (ALT_ROW_BG if row_index % 2 == 0 else ROW_BG)
        for column, value in enumerate(row):
            draw.rectangle((x, y, x + widths[column], y + row_height), fill=bg, outline=GRID)
            draw.text((x + CELL_PADDING_X, y + CELL_PADDING_Y), value, fill=TEXT, font=bold_font if row_index == 0 else font)
            x += widths[column]
        y += row_height
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return TableImage(filename=filename, data=output.getvalue())


def _looks_like_markdown_table_header(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if not _looks_like_table_row(header):
        return False
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?", separator))


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and not stripped.startswith("```")


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _truncate_cell(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_CELL_CHARS:
        return cleaned
    return cleaned[: MAX_CELL_CHARS - 1].rstrip() + "..."
