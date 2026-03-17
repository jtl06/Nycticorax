from __future__ import annotations


def format_ping_message(latency_seconds: float) -> str:
    latency_ms = round(max(latency_seconds, 0.0) * 1000)
    return f"Pong! `{latency_ms} ms`"


def format_latency_debug_block(metrics: dict[str, int]) -> str:
    ordered_keys = (
        "end_to_end_ms",
        "context_fetch_ms",
        "memory_retrieval_ms",
        "chat_llm_ms",
        "chat_usage_write_ms",
        "chat_commit_ms",
        "reply_generation_ms",
    )
    lines = ["latency_debug_ms"]
    for key in ordered_keys:
        if key in metrics:
            lines.append(f"{key}: {metrics[key]}")
    lines.append("memory_extraction: background")
    return "```text\n" + "\n".join(lines) + "\n```"


def append_debug_block(reply_text: str, debug_block: str, limit: int = 1900) -> str:
    suffix = "\n\n" + debug_block
    if len(reply_text) + len(suffix) <= limit:
        return reply_text + suffix
    trim_target = max(0, limit - len(suffix))
    if trim_target <= 3:
        return debug_block[:limit]
    trimmed = reply_text[: trim_target - 3].rstrip()
    return f"{trimmed}...{suffix}"
