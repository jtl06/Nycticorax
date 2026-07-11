from __future__ import annotations

from functools import lru_cache
import ssl
from typing import Any
from urllib.request import Request, urlopen as _stdlib_urlopen

import certifi


@lru_cache(maxsize=1)
def http_ssl_context() -> ssl.SSLContext:
    """Return platform trust augmented with certifi's public CA bundle."""

    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def urlopen(request: str | Request, *, timeout: float) -> Any:
    """Open an HTTP request with portable platform and public CA trust."""

    return _stdlib_urlopen(
        request,
        timeout=timeout,
        context=http_ssl_context(),
    )
