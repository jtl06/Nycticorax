import unittest

from nycti.formatting import model_requires_data_uri_image_input


class FormattingImageTests(unittest.TestCase):
    def test_detects_clarifai_gemini_models_as_requiring_data_uris(self) -> None:
        self.assertTrue(
            model_requires_data_uri_image_input(
                "https://clarifai.com/gcp/generate/models/gemini-3_1-flash-lite-preview"
            )
        )
        self.assertTrue(
            model_requires_data_uri_image_input(
                "https://clarifai.com/gcp/generate/models/gemini-3-flash-preview/versions/abc123"
            )
        )

    def test_non_gemini_models_do_not_require_data_uris(self) -> None:
        self.assertFalse(
            model_requires_data_uri_image_input(
                "https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5"
            )
        )
        self.assertFalse(model_requires_data_uri_image_input("gpt-4.1-mini"))
        self.assertFalse(model_requires_data_uri_image_input(None))


if __name__ == "__main__":
    unittest.main()
