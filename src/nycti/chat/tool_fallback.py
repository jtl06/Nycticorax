from __future__ import annotations


def fallback_tool_result(tool_result: str) -> str:
    if tool_result.startswith("Older Discord channel context (raw"):
        return (
            "I fetched older channel context, but failed to synthesize it cleanly. "
            "Try asking for a narrower summary or exact detail."
        )
    if tool_result.startswith("Tavily web results for:"):
        return (
            "I pulled web search sources but couldn't synthesize a clean final answer. "
            "Please retry with a narrower question."
        )
    if tool_result.startswith("Tavily extract for:"):
        return (
            "I extracted the page content but couldn't synthesize it cleanly. "
            "Please retry with a narrower ask."
        )
    if tool_result.startswith("YouTube transcript for:"):
        return (
            "I extracted the YouTube transcript but couldn't synthesize it cleanly. "
            "Please retry with a narrower question about the video."
        )
    return tool_result
