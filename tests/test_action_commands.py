from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nycti.chat.action_confirmation import ActionConfirmationError
from nycti.discord.actions import (
    confirm_action_proposal,
    format_confirmation_failure,
    normalize_proposal_id,
    register_action_commands,
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

    async def test_confirm_does_not_send_followup_after_action_429(self) -> None:
        from nycti.discord.rate_limits import DiscordRateLimitCircuitBreaker

        class FakeRateLimit(Exception):
            status = 429

        class FakeTree:
            def __init__(self) -> None:
                self.commands: dict[str, object] = {}

            def command(self, *, name: str, **_kwargs):  # type: ignore[no-untyped-def]
                def decorator(callback):  # type: ignore[no-untyped-def]
                    self.commands[name] = callback
                    return callback

                return decorator

        breaker = DiscordRateLimitCircuitBreaker(default_cooldown_seconds=60)
        tree = FakeTree()
        bot = SimpleNamespace(tree=tree)
        register_action_commands(bot)
        interaction = SimpleNamespace(
            guild_id=1,
            channel_id=2,
            user=SimpleNamespace(id=3),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def trip_rate_limit(*_args, **_kwargs) -> str:
            breaker.record_exception(FakeRateLimit())
            return "Channel send paused."

        with (
            patch("nycti.discord.actions.DISCORD_OUTBOUND_CIRCUIT_BREAKER", breaker),
            patch("nycti.discord.actions.confirm_action_proposal", side_effect=trip_rate_limit),
        ):
            await tree.commands["confirm"](interaction, "act_123")  # type: ignore[operator]

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
