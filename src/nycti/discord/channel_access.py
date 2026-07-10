from __future__ import annotations

from typing import Any


async def member_can_view_channel(channel: Any, member: Any) -> bool:
    """Resolve Discord read access, including private-thread membership."""

    permissions = _permissions_for(channel, member)
    if permissions is None or not _can_view(permissions):
        return False
    if not _is_thread(channel) or not _thread_is_private(channel):
        return True
    if bool(getattr(permissions, "manage_threads", False)):
        return True
    return await _is_thread_member(channel, getattr(member, "id", None))


async def member_can_send_to_channel(channel: Any, member: Any) -> bool:
    """Resolve Discord send access without letting the bot bypass thread ACLs."""

    permissions = _permissions_for(channel, member)
    if permissions is None or not _can_view(permissions):
        return False
    if not _is_thread(channel):
        return bool(getattr(permissions, "send_messages", False))
    if not bool(getattr(permissions, "send_messages_in_threads", False)):
        return False
    can_manage_threads = bool(getattr(permissions, "manage_threads", False))
    if (
        bool(getattr(channel, "locked", False))
        or bool(getattr(channel, "archived", False))
    ) and not can_manage_threads:
        return False
    if _thread_is_private(channel) and not can_manage_threads:
        return await _is_thread_member(channel, getattr(member, "id", None))
    return True


def _permissions_for(channel: Any, member: Any) -> Any | None:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for) or member is None:
        return None
    try:
        return permissions_for(member)
    except Exception:
        return None


def _can_view(permissions: Any) -> bool:
    return bool(
        getattr(
            permissions,
            "view_channel",
            getattr(permissions, "read_messages", False),
        )
    )


def _is_thread(channel: Any) -> bool:
    # ``Thread.permissions_for`` only resolves parent-channel permissions. The
    # presence of the thread-specific API is therefore the relevant boundary,
    # and keeps this helper testable without constructing discord.py internals.
    return callable(getattr(channel, "is_private", None)) and hasattr(channel, "locked")


def _thread_is_private(channel: Any) -> bool:
    is_private = getattr(channel, "is_private", None)
    try:
        return bool(is_private()) if callable(is_private) else False
    except Exception:
        return True


async def _is_thread_member(channel: Any, member_id: Any) -> bool:
    if not isinstance(member_id, int) or isinstance(member_id, bool) or member_id <= 0:
        return False
    for thread_member in getattr(channel, "members", ()) or ():
        if getattr(thread_member, "id", None) == member_id:
            return True
    fetch_member = getattr(channel, "fetch_member", None)
    if not callable(fetch_member):
        return False
    try:
        fetched = await fetch_member(member_id)
    except Exception:
        return False
    return getattr(fetched, "id", None) == member_id
