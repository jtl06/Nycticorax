from types import SimpleNamespace
import unittest

from nycti.chat.action_confirmation import ActionConfirmationError
from nycti.discord.actions import (
    confirm_action_proposal,
    format_confirmation_failure,
    normalize_proposal_id,
)


class _Executor:
    def __init__(self) -> None:
        self.calls: list[dict[str, int | str]] = []

    async def confirm_action(self, proposal_id: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"proposal_id": proposal_id, **kwargs})
        return "Confirmed."


class ActionCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_proposal_id_accepts_display_form(self) -> None:
        self.assertEqual("act_123", normalize_proposal_id("`proposal:act_123`"))

    async def test_confirmation_uses_exact_interaction_identity(self) -> None:
        executor = _Executor()
        bot = SimpleNamespace(
            _chat_orchestrator=SimpleNamespace(
                tool_runner=SimpleNamespace(executor=executor),
            )
        )

        result = await confirm_action_proposal(
            bot,
            proposal_id="proposal:act_123",
            guild_id=1,
            channel_id=2,
            user_id=3,
        )

        self.assertEqual("Confirmed.", result)
        self.assertEqual(
            [{"proposal_id": "act_123", "guild_id": 1, "channel_id": 2, "user_id": 3}],
            executor.calls,
        )

    def test_timeout_and_unknown_failures_never_claim_nothing_executed(self) -> None:
        timeout_message = format_confirmation_failure(TimeoutError())
        unknown_message = format_confirmation_failure(RuntimeError("lost acknowledgement"))

        self.assertIn("status is unknown", timeout_message)
        self.assertIn("check", timeout_message.casefold())
        self.assertNotIn("before the action could execute", timeout_message)
        self.assertIn("may have completed", unknown_message)
        self.assertIn("check", unknown_message.casefold())
        self.assertNotIn("failed safely", unknown_message)

    def test_known_confirmation_error_remains_precise(self) -> None:
        error = ActionConfirmationError("wrong_user", "Only the requester can confirm this.")

        self.assertEqual("Only the requester can confirm this.", format_confirmation_failure(error))


if __name__ == "__main__":
    unittest.main()
