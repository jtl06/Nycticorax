import unittest

from nycti.timezones import DEFAULT_TIMEZONE_NAME, canonicalize_timezone_name, resolve_timezone_name


class TimezoneTests(unittest.TestCase):
    def test_canonicalize_timezone_name_maps_pst_to_pacific(self) -> None:
        self.assertEqual(canonicalize_timezone_name("PST"), DEFAULT_TIMEZONE_NAME)

    def test_canonicalize_timezone_name_accepts_iana_name(self) -> None:
        self.assertEqual(canonicalize_timezone_name("UTC"), "UTC")

    def test_resolve_timezone_name_falls_back_to_default(self) -> None:
        self.assertEqual(resolve_timezone_name("not-a-real-zone"), DEFAULT_TIMEZONE_NAME)


if __name__ == "__main__":
    unittest.main()
