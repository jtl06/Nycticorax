import unittest
from io import BytesIO

from PIL import Image

from nycti.table_images import _clean_cell, extract_markdown_tables_as_images, render_markdown_table_image


class TableImageTests(unittest.TestCase):
    def test_extract_markdown_tables_as_images_replaces_table_with_attachment_marker(self) -> None:
        result = extract_markdown_tables_as_images(
            "here\n\n| Name | Value |\n| --- | ---: |\n| A | 123 |\n\ndone"
        )

        self.assertIn("[attached table image: table-1.png]", result.text)
        self.assertNotIn("| Name | Value |", result.text)
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].filename, "table-1.png")
        self.assertTrue(result.images[0].data.startswith(b"\x89PNG"))

    def test_extract_markdown_tables_as_images_ignores_code_fences(self) -> None:
        result = extract_markdown_tables_as_images(
            "```text\n| Name | Value |\n| --- | --- |\n| A | 123 |\n```"
        )

        self.assertEqual(result.images, [])
        self.assertIn("| Name | Value |", result.text)

    def test_render_markdown_table_image_wraps_long_cells_into_readable_rows(self) -> None:
        image = render_markdown_table_image(
            [
                "| Company | EBITDA (2024) | Employees (end 2024) |",
                "| --- | --- | --- |",
                "| **Meta Platforms** | Not disclosed in the sources consulted; filing reports operating income instead. | 74,067 employees (10% YoY increase) |",
                "| **Alphabet (Google)** | **$127.7B** annual EBITDA | 183,323 employees (0.45% YoY increase) |",
            ]
        )

        with Image.open(BytesIO(image.data)) as rendered:
            self.assertGreaterEqual(rendered.width, 600)
            self.assertGreaterEqual(rendered.height, 90)
            self.assertLess(rendered.height, 220)

    def test_clean_cell_strips_markdown_and_citation_artifacts(self) -> None:
        self.assertEqual(
            _clean_cell("**Alphabet (Google)** citeturn0search0 "),
            "Alphabet (Google)",
        )
        self.assertEqual(
            _clean_cell("Netincome per employee (202425)"),
            "Net income per employee (2024 25)",
        )

    def test_clean_cell_normalizes_financial_symbols_for_image_fonts(self) -> None:
        self.assertEqual(
            _clean_cell("1.1\u202fM ÷ $37 ≈ 29,730 shares"),
            "1.1 M / $37 ~ 29,730 shares",
        )
        self.assertEqual(
            _clean_cell("29,730 × $1,562 ≈ $46.9\u202fM"),
            "29,730 x $1,562 ~ $46.9 M",
        )
        self.assertEqual(
            _clean_cell("down $16.55 (‑2.78 %) → after-hours"),
            "down $16.55 (-2.78 %) -> after-hours",
        )

    def test_render_markdown_table_image_keeps_finance_table_readable(self) -> None:
        image = render_markdown_table_image(
            [
                "| Investment style | Approx. $1.1 M exposure | Result by May 2026 |",
                "| --- | --- | --- |",
                (
                    "| Buy the stock outright | 1.1 M ÷ $37 ≈ 29,730 shares "
                    "| 29,730 × $1,562 ≈ $46.9 M |"
                ),
                (
                    "| 2:1 margin (borrow equal amount) "
                    "| $2.2 M buying power ≈ 59,460 shares "
                    "| 59,460 × $1,562 ≈ $93.8 M |"
                ),
            ]
        )

        with Image.open(BytesIO(image.data)) as rendered:
            self.assertGreaterEqual(rendered.width, 700)
            self.assertGreaterEqual(rendered.height, 120)
            self.assertLess(rendered.width, 1200)


if __name__ == "__main__":
    unittest.main()
