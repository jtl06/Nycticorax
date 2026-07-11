from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from nycti import http_tls


class PortableTLSOpenerTests(unittest.TestCase):
    def tearDown(self) -> None:
        http_tls.http_ssl_context.cache_clear()

    def test_augments_platform_trust_with_certifi(self) -> None:
        context = Mock()
        with (
            patch.object(http_tls.ssl, "create_default_context", return_value=context) as create,
            patch.object(http_tls.certifi, "where", return_value="/certifi/ca.pem"),
        ):
            result = http_tls.http_ssl_context()

        self.assertIs(context, result)
        create.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="/certifi/ca.pem")

    def test_urlopen_uses_cached_merged_context(self) -> None:
        context = Mock()
        with (
            patch.object(http_tls, "http_ssl_context", return_value=context),
            patch.object(http_tls, "_stdlib_urlopen", return_value="response") as opener,
        ):
            response = http_tls.urlopen("https://example.com", timeout=3.0)

        self.assertEqual("response", response)
        opener.assert_called_once_with(
            "https://example.com",
            timeout=3.0,
            context=context,
        )


if __name__ == "__main__":
    unittest.main()
