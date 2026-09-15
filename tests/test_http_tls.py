from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import Mock, patch

from nycti import http_tls


class PortableTLSOpenerTests(unittest.TestCase):
    def setUp(self) -> None:
        http_tls._cached_http_ssl_context.cache_clear()

    def tearDown(self) -> None:
        http_tls._cached_http_ssl_context.cache_clear()

    def test_concurrent_first_use_constructs_only_one_trust_store(self) -> None:
        barrier = threading.Barrier(12)

        def create():
            time.sleep(0.02)
            return Mock()

        def acquire(_):
            barrier.wait(timeout=2)
            return http_tls.http_ssl_context()

        with patch.object(http_tls.ssl, "create_default_context", side_effect=create) as factory:
            with ThreadPoolExecutor(max_workers=12) as executor:
                contexts = list(executor.map(acquire, range(12)))
        factory.assert_called_once_with()
        self.assertTrue(all(context is contexts[0] for context in contexts))
        contexts[0].load_verify_locations.assert_called_once()

    def test_failed_initialization_releases_lock_and_can_retry(self) -> None:
        context = Mock()
        with patch.object(http_tls.ssl, "create_default_context", side_effect=[OSError("trust store"), context]):
            with self.assertRaises(OSError):
                http_tls.http_ssl_context()
            self.assertIs(context, http_tls.http_ssl_context())

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
