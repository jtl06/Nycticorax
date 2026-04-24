import unittest

from nycti.table_images import extract_markdown_tables_as_images


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


if __name__ == "__main__":
    unittest.main()
