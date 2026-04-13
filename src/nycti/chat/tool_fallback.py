from __future__ import annotations


def fallback_tool_result(tool_result: str) -> str:
    if tool_result.startswith("Older Discord channel context (raw"):
        return (
            "I fetched older channel context, but failed to synthesize it cleanly. "
            "Try asking for a narrower summary or exact detail."
        )
    return tool_result
