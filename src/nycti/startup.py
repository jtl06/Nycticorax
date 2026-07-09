from __future__ import annotations

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - test environments may not install discord.py
    discord = None  # type: ignore[assignment]

MAX_DISCORD_START_RETRIES = 6
DISCORD_START_BACKOFF_BASE_SECONDS = 15
DISCORD_START_BACKOFF_MAX_SECONDS = 300


def is_retryable_discord_start_error(exc: Exception) -> bool:
    is_discord_http_error = discord is not None and isinstance(exc, discord.HTTPException)
    if not is_discord_http_error and not hasattr(exc, "status"):
        return False
    status = getattr(exc, "status", None)
    message = str(exc).lower()
    return bool(
        status == 429
        or "cloudflare" in message
        or "1015" in message
        or "rate limited" in message
        or "temporarily from accessing" in message
    )


def compute_discord_start_backoff_seconds(attempt: int) -> int:
    normalized_attempt = max(attempt, 1) - 1
    delay = DISCORD_START_BACKOFF_BASE_SECONDS * (2**normalized_attempt)
    return min(delay, DISCORD_START_BACKOFF_MAX_SECONDS)
