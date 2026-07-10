from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import unquote, urlsplit

from nycti.browser.models import (
    BrowserExtractResult,
    BrowserToolDataError,
    BrowserToolDisabledError,
    BrowserToolRuntimeError,
    BrowserToolUnavailableError,
)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HostResolver = Callable[[str], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class _BrowserPageData:
    final_url: str
    title: str
    extracted_text: Any


@dataclass(frozen=True, slots=True)
class _ValidatedDestination:
    normalized_url: str
    hostname: str
    addresses: tuple[str, ...]


class BrowserClient:
    def __init__(
        self,
        *,
        enabled: bool,
        timeout_seconds: float,
        headless: bool,
        allow_headed: bool,
        max_content_chars: int = 6000,
        resolve_hostname: HostResolver | None = None,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.allow_headed = allow_headed
        self.max_content_chars = max_content_chars
        self._resolve_hostname = resolve_hostname or _default_resolve_hostname

    async def extract(
        self,
        *,
        url: str,
        query: str | None = None,
        headed: bool = False,
    ) -> BrowserExtractResult:
        requested_url = url.strip()
        if not requested_url:
            raise BrowserToolDataError("Browser extract URL cannot be empty.")
        if not self.enabled:
            raise BrowserToolDisabledError("Browser extraction is disabled by configuration.")
        if headed and not self.allow_headed:
            raise BrowserToolDisabledError(
                "Headed browser mode is disabled. Set BROWSER_TOOL_ALLOW_HEADED=true to allow it."
            )

        destination = await self._validate_public_destination(requested_url)
        normalized_url = destination.normalized_url
        launch_headless = self.headless if not headed else False
        timeout_ms = max(int(self.timeout_seconds * 1000), 1000)
        page_data = await self._navigate(
            destination,
            launch_headless=launch_headless,
            timeout_ms=timeout_ms,
        )
        final_url = self._validate_same_host_url(
            page_data.final_url or normalized_url,
            allowed_hostname=destination.hostname,
        )
        title = " ".join(page_data.title.split()).strip()
        extracted_text = page_data.extracted_text

        content = _normalize_content(extracted_text)
        if query:
            content = _focus_content(content, query)
        content = content[: self.max_content_chars].strip()
        if not content and not title:
            raise BrowserToolDataError(
                f"Browser extract for `{normalized_url}` loaded, but no readable content was found."
            )

        return BrowserExtractResult(
            requested_url=normalized_url,
            final_url=final_url,
            title=title,
            content=content,
        )

    async def _validate_public_url(self, url: str) -> str:
        return (await self._validate_public_destination(url)).normalized_url

    async def _validate_public_destination(self, url: str) -> _ValidatedDestination:
        normalized_url, hostname = _parse_http_url(url)
        literal_address = _parse_ip_address(hostname)
        if literal_address is not None:
            _require_public_address(literal_address)
            return _ValidatedDestination(
                normalized_url=normalized_url,
                hostname=hostname,
                addresses=(str(literal_address),),
            )

        try:
            resolved_values = await self._resolve_hostname(hostname)
        except Exception as exc:
            raise BrowserToolDataError(
                f"Browser extract could not resolve destination host `{hostname}`."
            ) from exc
        if not resolved_values:
            raise BrowserToolDataError(
                f"Browser extract could not resolve destination host `{hostname}`."
            )
        addresses: list[str] = []
        for value in resolved_values:
            try:
                address = ipaddress.ip_address(str(value).split("%", 1)[0])
            except ValueError as exc:
                raise BrowserToolDataError(
                    f"Browser extract received an invalid address for destination host `{hostname}`."
                ) from exc
            _require_public_address(address)
            normalized_address = str(address)
            if normalized_address not in addresses:
                addresses.append(normalized_address)
        return _ValidatedDestination(
            normalized_url=normalized_url,
            hostname=hostname,
            addresses=tuple(addresses),
        )

    def _validate_same_host_url(self, url: str, *, allowed_hostname: str) -> str:
        normalized_url, hostname = _parse_http_url(url)
        if hostname != allowed_hostname:
            raise BrowserToolDataError(
                "Browser extract blocked a cross-host redirect or resource request."
            )
        return normalized_url

    async def _guard_outbound_request(
        self,
        route: Any,
        request: Any,
        *,
        allowed_hostname: str,
    ) -> BrowserToolDataError | None:
        try:
            self._validate_same_host_url(
                str(request.url),
                allowed_hostname=allowed_hostname,
            )
        except BrowserToolDataError as exc:
            await route.abort("blockedbyclient")
            return exc
        await route.continue_()
        return None

    async def _guard_navigation_request(
        self,
        route: Any,
        request: Any,
        *,
        allowed_hostname: str | None = None,
    ) -> BrowserToolDataError | None:
        """Compatibility wrapper for focused callers; all requests use the outbound guard."""
        if allowed_hostname is None:
            try:
                destination = await self._validate_public_destination(str(request.url))
            except BrowserToolDataError as exc:
                await route.abort("blockedbyclient")
                return exc
            allowed_hostname = destination.hostname
        return await self._guard_outbound_request(
            route,
            request,
            allowed_hostname=allowed_hostname,
        )

    async def _navigate(
        self,
        destination: _ValidatedDestination,
        *,
        launch_headless: bool,
        timeout_ms: int,
    ) -> _BrowserPageData:
        normalized_url = destination.normalized_url
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency/runtime guard
            raise BrowserToolUnavailableError(
                "Playwright is not installed. Install `playwright` and run `playwright install chromium`."
            ) from exc

        blocked_navigation: BrowserToolDataError | None = None

        async def guard_request(route: Any, request: Any) -> None:
            nonlocal blocked_navigation
            blocked = await self._guard_outbound_request(
                route,
                request,
                allowed_hostname=destination.hostname,
            )
            if blocked is not None and request.is_navigation_request():
                blocked_navigation = blocked_navigation or blocked

        try:
            async with async_playwright() as playwright:
                browser: Any | None = None
                context: Any | None = None
                try:
                    browser = await playwright.chromium.launch(
                        headless=launch_headless,
                        args=_chromium_launch_args(destination),
                    )
                    context = await browser.new_context(
                        user_agent=DEFAULT_BROWSER_USER_AGENT,
                        service_workers="block",
                    )
                    await context.route("**/*", guard_request)
                    await context.route_web_socket("**/*", _block_web_socket)
                    page = await context.new_page()
                    await page.goto(normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if blocked_navigation is not None:
                        raise blocked_navigation
                    self._validate_same_host_url(
                        (page.url or normalized_url).strip(),
                        allowed_hostname=destination.hostname,
                    )
                    try:
                        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
                    except PlaywrightTimeoutError:
                        # Some sites keep background requests open; domcontentloaded is enough.
                        pass
                    if blocked_navigation is not None:
                        raise blocked_navigation
                    final_url = self._validate_same_host_url(
                        (page.url or normalized_url).strip(),
                        allowed_hostname=destination.hostname,
                    )
                    title = " ".join((await page.title()).split()).strip()
                    extracted_text = await page.evaluate(_EXTRACTION_SCRIPT)
                finally:
                    if context is not None:
                        await _close_browser_resource(context)
                    if browser is not None:
                        await _close_browser_resource(browser)
        except BrowserToolDataError:
            raise
        except PlaywrightTimeoutError as exc:
            if blocked_navigation is not None:
                raise blocked_navigation from exc
            raise BrowserToolRuntimeError(
                f"Browser extract for `{normalized_url}` timed out before the page finished loading."
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive provider/runtime guard
            if blocked_navigation is not None:
                raise blocked_navigation from exc
            raise BrowserToolRuntimeError(
                f"Browser extract for `{normalized_url}` failed due to a Chromium runtime error."
            ) from exc
        return _BrowserPageData(
            final_url=final_url,
            title=title,
            extracted_text=extracted_text,
        )


def _parse_http_url(url: str) -> tuple[str, str]:
    normalized_url = url.strip()
    if not normalized_url or any(character.isspace() for character in normalized_url):
        raise BrowserToolDataError("Browser extract URL is invalid.")
    try:
        parsed = urlsplit(normalized_url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserToolDataError("Browser extract URL is invalid.") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise BrowserToolDataError("Browser extract only allows HTTP or HTTPS URLs.")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserToolDataError("Browser extract URLs cannot include credentials.")
    if not parsed.hostname:
        raise BrowserToolDataError("Browser extract URL must include a hostname.")
    if port is not None and (port < 1 or port > 65535):
        raise BrowserToolDataError("Browser extract URL contains an invalid port.")

    decoded_hostname = unquote(parsed.hostname)
    if decoded_hostname.endswith("."):
        raise BrowserToolDataError("Browser extract URL contains an ambiguous hostname.")
    hostname = decoded_hostname.casefold()
    if (
        not hostname
        or any(character.isspace() or ord(character) < 32 for character in hostname)
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        raise BrowserToolDataError("Browser extract cannot access a local or non-public destination.")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BrowserToolDataError("Browser extract URL contains an invalid hostname.") from exc
    if not re.fullmatch(r"[a-z0-9._:-]+", hostname):
        raise BrowserToolDataError("Browser extract URL contains an invalid hostname.")
    return normalized_url, hostname


def _chromium_launch_args(destination: _ValidatedDestination) -> list[str]:
    return [
        "--disable-background-networking",
        "--disable-quic",
        "--dns-prefetch-disable",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-proxy-server",
        f"--host-resolver-rules={_chromium_resolver_rules(destination)}",
    ]


def _chromium_resolver_rules(destination: _ValidatedDestination) -> str:
    if _parse_ip_address(destination.hostname) is not None:
        return f"EXCLUDE {destination.hostname}, MAP * ~NOTFOUND"
    pinned_address = next(
        (
            value
            for value in destination.addresses
            if isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
        ),
        destination.addresses[0],
    )
    parsed_address = ipaddress.ip_address(pinned_address)
    replacement = f"[{pinned_address}]" if isinstance(parsed_address, ipaddress.IPv6Address) else pinned_address
    return f"MAP {destination.hostname} {replacement}, MAP * ~NOTFOUND"


async def _default_resolve_hostname(hostname: str) -> tuple[str, ...]:
    def resolve() -> tuple[str, ...]:
        records = socket.getaddrinfo(
            hostname,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(dict.fromkeys(str(record[4][0]) for record in records))

    return await asyncio.to_thread(resolve)


async def _close_browser_resource(resource: Any) -> None:
    try:
        await resource.close()
    except Exception:
        # Cleanup must not hide the navigation or validation error.
        pass


async def _block_web_socket(route: Any) -> None:
    await route.close(code=1008, reason="Browser extraction blocks WebSocket connections.")


def _parse_ip_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = hostname.strip()
    try:
        return ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        pass

    parts = candidate.rstrip(".").split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        normalized = part.casefold()
        try:
            if normalized.startswith("0x") and len(normalized) > 2:
                values.append(int(normalized[2:], 16))
            elif len(normalized) > 1 and normalized.startswith("0"):
                values.append(int(normalized[1:] or "0", 8))
            elif normalized.isdecimal():
                values.append(int(normalized, 10))
            else:
                return None
        except ValueError:
            return None
    if any(value > 255 for value in values[:-1]):
        return None
    last_bits = 8 * (5 - len(values))
    if values[-1] >= 1 << last_bits:
        return None
    address_value = values[-1]
    for index, value in enumerate(values[:-1]):
        address_value += value << (8 * (3 - index))
    return ipaddress.IPv4Address(address_value)


def _require_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise BrowserToolDataError(
            "Browser extract cannot access a local, private, reserved, or otherwise non-public destination."
        )


def _normalize_content(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _focus_content(content: str, query: str) -> str:
    normalized_query = " ".join(query.split()).strip().lower()
    if not normalized_query:
        return content
    query_terms = [term for term in re.split(r"[^a-z0-9]+", normalized_query) if len(term) >= 3]
    if not query_terms:
        return content
    selected_lines: list[str] = []
    for line in content.splitlines():
        normalized_line = line.lower()
        if any(term in normalized_line for term in query_terms):
            selected_lines.append(line.strip())
        if len(selected_lines) >= 20:
            break
    if not selected_lines:
        return content
    return "\n".join(selected_lines)


_EXTRACTION_SCRIPT = """
() => {
  const candidates = [
    "article",
    "main",
    "[role='main']",
    ".article-body",
    ".post-content",
    ".story-body",
    ".news-release",
    ".newsrelease-body",
    ".content"
  ];
  let root = null;
  for (const selector of candidates) {
    const el = document.querySelector(selector);
    if (el && el.innerText && el.innerText.trim().length > 120) {
      root = el;
      break;
    }
  }
  if (!root) {
    root = document.body;
  }
  const text = (root && root.innerText ? root.innerText : "").trim();
  const metaDescription = (document.querySelector("meta[name='description']") || {}).content || "";
  const heading = ((document.querySelector("h1") || {}).innerText || "").trim();
  const parts = [];
  if (heading) parts.push(heading);
  if (metaDescription) parts.push(metaDescription);
  if (text) parts.push(text);
  return parts.join("\\n\\n").trim();
}
"""
