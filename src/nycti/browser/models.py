from __future__ import annotations

from dataclasses import dataclass


class BrowserToolError(Exception):
    """Base exception for browser-tool failures."""


class BrowserToolDisabledError(BrowserToolError):
    """Raised when browser extraction is disabled by configuration."""


class BrowserToolUnavailableError(BrowserToolError):
    """Raised when browser dependencies/runtime are unavailable."""


class BrowserToolDataError(BrowserToolError):
    """Raised when extraction succeeds but returns no usable page content."""


class BrowserToolRuntimeError(BrowserToolError):
    """Raised when navigation or page interaction fails."""


@dataclass(frozen=True, slots=True)
class BrowserExtractResult:
    requested_url: str
    final_url: str
    title: str
    content: str
