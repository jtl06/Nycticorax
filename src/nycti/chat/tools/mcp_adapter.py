from __future__ import annotations

from typing import Any

from nycti.chat.tools.registry import get_tool_metadata
from nycti.chat.tools.schemas import build_chat_tools


def build_mcp_tool_descriptors() -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for tool in build_chat_tools():
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        metadata = get_tool_metadata(name)
        annotations: dict[str, object] = {}
        if metadata is not None:
            annotations = {
                "nycti/skill": metadata.skill,
                "nycti/cost": metadata.cost,
                "nycti/risk": metadata.risk,
                "nycti/required_env": list(metadata.required_env),
                "nycti/permission": metadata.permission,
                "nycti/fallback": metadata.fallback,
            }
        descriptors.append(
            {
                "name": name,
                "description": function.get("description", ""),
                "inputSchema": function.get("parameters", {"type": "object", "properties": {}}),
                "annotations": annotations,
            }
        )
    return descriptors
