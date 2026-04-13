import unittest

from nycti.permissions import can_manage_user_memories, can_view_user_memories


class MemoryCommandPermissionTests(unittest.TestCase):
    def test_user_can_view_own_memories(self) -> None:
        self.assertTrue(
            can_view_user_memories(
                requester_id=123,
                target_user_id=123,
                admin_user_id=None,
            )
        )

    def test_admin_can_view_other_user_memories(self) -> None:
        self.assertTrue(
            can_view_user_memories(
                requester_id=999,
                target_user_id=123,
                admin_user_id=999,
            )
        )

    def test_non_admin_cannot_view_other_user_memories(self) -> None:
        self.assertFalse(
            can_view_user_memories(
                requester_id=555,
                target_user_id=123,
                admin_user_id=999,
            )
        )

    def test_user_can_manage_own_memories(self) -> None:
        self.assertTrue(
            can_manage_user_memories(
                requester_id=123,
                target_user_id=123,
                admin_user_id=None,
            )
        )

    def test_admin_can_manage_other_user_memories(self) -> None:
        self.assertTrue(
            can_manage_user_memories(
                requester_id=999,
                target_user_id=123,
                admin_user_id=999,
            )
        )

    def test_non_admin_cannot_manage_other_user_memories(self) -> None:
        self.assertFalse(
            can_manage_user_memories(
                requester_id=555,
                target_user_id=123,
                admin_user_id=999,
            )
        )


if __name__ == "__main__":
    unittest.main()
