from __future__ import annotations


def can_view_user_memories(*, requester_id: int, target_user_id: int, admin_user_id: int | None) -> bool:
    return requester_id == target_user_id or requester_id == admin_user_id


def can_manage_user_memories(*, requester_id: int, target_user_id: int, admin_user_id: int | None) -> bool:
    return requester_id == target_user_id or requester_id == admin_user_id
