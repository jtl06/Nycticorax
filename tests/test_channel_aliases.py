import unittest

from nycti.channel_aliases import normalize_channel_alias


class ChannelAliasTests(unittest.TestCase):
    def test_normalize_channel_alias_lowercases_valid_aliases(self) -> None:
        self.assertEqual(normalize_channel_alias("Alerts_Prod"), "alerts_prod")

    def test_normalize_channel_alias_rejects_invalid_characters(self) -> None:
        self.assertIsNone(normalize_channel_alias("alerts prod"))

    def test_normalize_channel_alias_rejects_empty_aliases(self) -> None:
        self.assertIsNone(normalize_channel_alias(""))


if __name__ == "__main__":
    unittest.main()
