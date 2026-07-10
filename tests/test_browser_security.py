from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, patch

from nycti.browser.client import (
    BrowserClient,
    HostResolver,
    _block_web_socket,
    _BrowserPageData,
    _chromium_launch_args,
)
from nycti.browser.models import BrowserToolDataError


class BrowserURLSecurityTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, resolver: HostResolver | None = None) -> BrowserClient:
        return BrowserClient(
            enabled=True,
            timeout_seconds=5,
            headless=True,
            allow_headed=False,
            resolve_hostname=resolver or _resolver({"example.com": ("93.184.216.34",)}),
        )

    async def test_allows_public_http_and_https_destinations(self) -> None:
        resolver = _RecordingResolver({
            "example.com": ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        })
        client = self._client(resolver)

        self.assertEqual(
            "https://example.com/report",
            await client._validate_public_url("https://example.com/report"),
        )
        self.assertEqual(
            "http://93.184.216.34/data",
            await client._validate_public_url("http://93.184.216.34/data"),
        )
        self.assertEqual(["example.com"], resolver.hostnames)

    async def test_rejects_non_http_schemes_and_credentials(self) -> None:
        client = self._client()
        invalid_urls = (
            "file:///etc/passwd",
            "ftp://example.com/file",
            "data:text/plain,secret",
            "javascript:alert(1)",
            "//example.com/path",
            "https://user:password@example.com/private",
            "https://example%00.com/private",
            "https://example.com./private",
        )

        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(BrowserToolDataError):
                await client._validate_public_url(value)

    async def test_rejects_local_private_metadata_and_special_ip_literals(self) -> None:
        client = self._client(_resolver({}))
        unsafe_hosts = (
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "100.64.0.1",
            "192.0.2.1",
            "224.0.0.1",
            "240.0.0.1",
            "[::1]",
            "[fc00::1]",
            "[fe80::1]",
            "[ff02::1]",
            "[::]",
            "[::ffff:127.0.0.1]",
        )

        for host in unsafe_hosts:
            with self.subTest(host=host), self.assertRaises(BrowserToolDataError):
                await client._validate_public_url(f"http://{host}/secret")

    async def test_rejects_alternate_numeric_loopback_forms(self) -> None:
        client = self._client(_resolver({}))
        unsafe_hosts = (
            "2130706433",
            "0x7f000001",
            "017700000001",
            "127.1",
            "127.0.1",
            "0x7f.1",
            "%31%32%37.0.0.1",
        )

        for host in unsafe_hosts:
            with self.subTest(host=host), self.assertRaises(BrowserToolDataError):
                await client._validate_public_url(f"http://{host}/secret")

    async def test_rejects_dns_hosts_with_any_non_public_address(self) -> None:
        resolver = _resolver({
            "localhost.test": ("127.0.0.1",),
            "metadata.google.internal": ("169.254.169.254",),
            "mixed.example": ("93.184.216.34", "10.0.0.4"),
        })
        client = self._client(resolver)

        for host in ("localhost.test", "metadata.google.internal", "mixed.example"):
            with self.subTest(host=host), self.assertRaises(BrowserToolDataError):
                await client._validate_public_url(f"https://{host}/")

    async def test_rejects_localhost_without_resolving_it(self) -> None:
        resolver = AsyncMock(return_value=("93.184.216.34",))
        client = self._client(resolver)

        with self.assertRaises(BrowserToolDataError):
            await client._validate_public_url("http://localhost/admin")

        resolver.assert_not_awaited()

    async def test_extract_revalidates_redirect_destination(self) -> None:
        client = self._client()
        navigation = AsyncMock(return_value=_BrowserPageData(
            final_url="http://169.254.169.254/latest/meta-data/",
            title="metadata",
            extracted_text="secret",
        ))

        with patch.object(client, "_navigate", navigation):
            with self.assertRaises(BrowserToolDataError):
                await client.extract(url="https://example.com/start")

        navigation.assert_awaited_once()

    async def test_extract_rejects_private_request_before_navigation(self) -> None:
        client = self._client()
        navigation = AsyncMock()

        with patch.object(client, "_navigate", navigation):
            with self.assertRaises(BrowserToolDataError):
                await client.extract(url="http://127.0.0.1/admin")

        navigation.assert_not_awaited()

    async def test_navigation_guard_aborts_private_redirect_before_following_it(self) -> None:
        client = self._client()
        route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
        request = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            is_navigation_request=lambda: True,
        )

        blocked = await client._guard_navigation_request(route, request)

        self.assertIsInstance(blocked, BrowserToolDataError)
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()

    async def test_outbound_guard_blocks_private_non_navigation_request(self) -> None:
        client = self._client()
        route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
        request = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            is_navigation_request=lambda: False,
        )

        blocked = await client._guard_outbound_request(
            route,
            request,
            allowed_hostname="example.com",
        )

        self.assertIsInstance(blocked, BrowserToolDataError)
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()

    async def test_outbound_guard_allows_only_same_host_resources(self) -> None:
        client = self._client()
        same_host_route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
        cross_host_route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())

        same_host = await client._guard_outbound_request(
            same_host_route,
            SimpleNamespace(url="https://example.com/app.js"),
            allowed_hostname="example.com",
        )
        cross_host = await client._guard_outbound_request(
            cross_host_route,
            SimpleNamespace(url="https://cdn.example.net/app.js"),
            allowed_hostname="example.com",
        )

        self.assertIsNone(same_host)
        same_host_route.continue_.assert_awaited_once()
        self.assertIsInstance(cross_host, BrowserToolDataError)
        cross_host_route.abort.assert_awaited_once_with("blockedbyclient")

    async def test_chromium_dns_is_pinned_and_other_hosts_are_disabled(self) -> None:
        client = self._client(_resolver({
            "example.com": ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        }))

        destination = await client._validate_public_destination("https://example.com/report")
        launch_args = _chromium_launch_args(destination)

        self.assertIn("--no-proxy-server", launch_args)
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            launch_args,
        )

    async def test_navigation_launches_chromium_with_pin_and_request_guards(self) -> None:
        client = self._client()
        destination = await client._validate_public_destination("https://example.com/report")
        page = SimpleNamespace(
            url="https://example.com/final",
            goto=AsyncMock(),
            wait_for_load_state=AsyncMock(),
            title=AsyncMock(return_value="Example"),
            evaluate=AsyncMock(return_value="Content"),
        )
        context = SimpleNamespace(
            route=AsyncMock(),
            route_web_socket=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(),
        )
        browser = SimpleNamespace(
            new_context=AsyncMock(return_value=context),
            close=AsyncMock(),
        )
        launch = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        with patch(
            "playwright.async_api.async_playwright",
            return_value=_AsyncContextManager(playwright),
        ):
            result = await client._navigate(
                destination,
                launch_headless=True,
                timeout_ms=5000,
            )

        launch_call = launch.await_args
        assert launch_call is not None
        launch_args = launch_call.kwargs["args"]
        self.assertIn(
            "--host-resolver-rules=MAP example.com 93.184.216.34, MAP * ~NOTFOUND",
            launch_args,
        )
        browser.new_context.assert_awaited_once_with(
            user_agent=ANY,
            service_workers="block",
        )
        context.route.assert_awaited_once()
        context.route_web_socket.assert_awaited_once()
        self.assertEqual("https://example.com/final", result.final_url)
        self.assertIn(
            "--host-resolver-rules=MAP example.com 93.184.216.34, MAP * ~NOTFOUND",
            launch_args,
        )

    async def test_web_sockets_are_blocked(self) -> None:
        route = SimpleNamespace(close=AsyncMock())

        await _block_web_socket(route)

        route.close.assert_awaited_once_with(
            code=1008,
            reason="Browser extraction blocks WebSocket connections.",
        )

    async def test_extract_preserves_normal_public_redirect_behavior(self) -> None:
        resolver = _resolver({"example.com": ("93.184.216.34",)})
        client = self._client(resolver)
        navigation = AsyncMock(return_value=_BrowserPageData(
            final_url="https://example.com/final",
            title=" Example  Report ",
            extracted_text=" Public  report content. ",
        ))

        with patch.object(client, "_navigate", navigation):
            result = await client.extract(url="https://example.com/start")

        self.assertEqual("https://example.com/start", result.requested_url)
        self.assertEqual("https://example.com/final", result.final_url)
        self.assertEqual("Example Report", result.title)
        self.assertEqual("Public report content.", result.content)

    async def test_extract_rejects_cross_host_public_redirect(self) -> None:
        client = self._client()
        navigation = AsyncMock(return_value=_BrowserPageData(
            final_url="https://www.example.com/final",
            title="Redirected",
            extracted_text="content",
        ))

        with patch.object(client, "_navigate", navigation):
            with self.assertRaises(BrowserToolDataError):
                await client.extract(url="https://example.com/start")


class _RecordingResolver:
    def __init__(self, records: dict[str, Sequence[str]]) -> None:
        self.records = records
        self.hostnames: list[str] = []

    async def __call__(self, hostname: str) -> Sequence[str]:
        self.hostnames.append(hostname)
        return self.records[hostname]


class _AsyncContextManager:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


def _resolver(records: dict[str, Sequence[str]]) -> HostResolver:
    async def resolve(hostname: str) -> Sequence[str]:
        return records[hostname]

    return resolve
