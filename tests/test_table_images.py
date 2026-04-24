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
            self.assertLess(rendered.height, 180)

    def test_clean_cell_strips_markdown_and_citation_artifacts(self) -> None:
        self.assertEqual(
            _clean_cell("**Alphabet (Google)** citeturn0search0 "),
            "Alphabet (Google)",
        )
        self.assertEqual(
            _clean_cell("Netincome per employee (202425)"),
            "Net income per employee (2024 25)",
        )


if __name__ == "__main__":
    unittest.main()
