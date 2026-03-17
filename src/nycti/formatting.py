from __future__ import annotations


def format_ping_message(latency_seconds: float) -> str:
    latency_ms = round(max(latency_seconds, 0.0) * 1000)
    return f"Pong! `{latency_ms} ms`"
