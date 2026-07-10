import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from nycti.chat.action_confirmation import (
    ActionConfirmationError,
    ActionConfirmationStore,
    ActionKind,
    ChannelMessageAction,
    ReminderAction,
    append_authoritative_action_cards,
    render_action_proposal_card,
)
from nycti.chat.run_state import ToolOutcome, ToolStatus


class ActionConfirmationStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        self.store = ActionConfirmationStore(
            proposal_ttl=timedelta(minutes=5),
            capability_ttl=timedelta(seconds=30),
            now=lambda: self.now,
        )

    async def _proposal(self):  # type: ignore[no-untyped-def]
        return await self.store.propose(
            kind=ActionKind.SEND_CHANNEL_MESSAGE,
            payload=ChannelMessageAction(target_channel_id=999, message_text="Deploy is live."),
            guild_id=1,
            request_channel_id=10,
            user_id=20,
            source_message_id=30,
        )

    async def test_confirmation_mints_bound_single_use_capability(self) -> None:
        proposal = await self._proposal()

        capability = await self.store.confirm(
            proposal.proposal_id,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )
        consumed = await self.store.consume(
            capability.token,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )

        self.assertEqual(proposal, consumed)
        with self.assertRaisesRegex(ActionConfirmationError, "invalid, expired, or already used"):
            await self.store.consume(
                capability.token,
                guild_id=1,
                channel_id=10,
                user_id=20,
            )

    async def test_wrong_user_or_channel_cannot_confirm(self) -> None:
        proposal = await self._proposal()

        with self.assertRaisesRegex(ActionConfirmationError, "Only the user"):
            await self.store.confirm(
                proposal.proposal_id,
                guild_id=1,
                channel_id=10,
                user_id=21,
            )
        with self.assertRaisesRegex(ActionConfirmationError, "same server and channel"):
            await self.store.confirm(
                proposal.proposal_id,
                guild_id=1,
                channel_id=11,
                user_id=20,
            )
        with self.assertRaisesRegex(ActionConfirmationError, "same server and channel"):
            await self.store.confirm(
                proposal.proposal_id,
                guild_id=2,
                channel_id=10,
                user_id=20,
            )

    async def test_wrong_identity_does_not_burn_capability(self) -> None:
        proposal = await self._proposal()
        capability = await self.store.confirm(
            proposal.proposal_id,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )

        with self.assertRaisesRegex(ActionConfirmationError, "Only the user"):
            await self.store.consume(
                capability.token,
                guild_id=1,
                channel_id=10,
                user_id=21,
            )

        consumed = await self.store.consume(
            capability.token,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )
        self.assertEqual(proposal, consumed)

    async def test_concurrent_confirmation_mints_only_one_capability(self) -> None:
        proposal = await self._proposal()

        results = await asyncio.gather(
            *(
                self.store.confirm(
                    proposal.proposal_id,
                    guild_id=1,
                    channel_id=10,
                    user_id=20,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

        capabilities = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, ActionConfirmationError)]
        self.assertEqual(1, len(capabilities))
        self.assertEqual(1, len(failures))

    async def test_pending_limit_is_per_principal_and_never_evicts_another_user(self) -> None:
        store = ActionConfirmationStore(max_pending=1, now=lambda: self.now)
        first = await store.propose(
            kind=ActionKind.SEND_CHANNEL_MESSAGE,
            payload=ChannelMessageAction(target_channel_id=999, message_text="First"),
            guild_id=1,
            request_channel_id=10,
            user_id=20,
            source_message_id=30,
        )

        with self.assertRaisesRegex(ActionConfirmationError, "maximum number"):
            await store.propose(
                kind=ActionKind.SEND_CHANNEL_MESSAGE,
                payload=ChannelMessageAction(target_channel_id=999, message_text="Second"),
                guild_id=1,
                request_channel_id=10,
                user_id=20,
                source_message_id=31,
            )
        other_user = await store.propose(
            kind=ActionKind.SEND_CHANNEL_MESSAGE,
            payload=ChannelMessageAction(target_channel_id=999, message_text="Other"),
            guild_id=1,
            request_channel_id=10,
            user_id=21,
            source_message_id=32,
        )

        self.assertNotEqual(first.proposal_id, other_user.proposal_id)
        capability = await store.confirm(
            first.proposal_id,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )
        self.assertEqual(first, capability.proposal)

    async def test_store_rejects_mismatched_payload_and_invalid_identity_binding(self) -> None:
        with self.assertRaisesRegex(ActionConfirmationError, "exact reminder payload"):
            await self.store.propose(
                kind=ActionKind.CREATE_REMINDER,
                payload=ChannelMessageAction(target_channel_id=999, message_text="Not a reminder"),
                guild_id=1,
                request_channel_id=10,
                user_id=20,
                source_message_id=30,
            )
        with self.assertRaisesRegex(ActionConfirmationError, "positive Discord IDs"):
            await self.store.propose(
                kind=ActionKind.SEND_CHANNEL_MESSAGE,
                payload=ChannelMessageAction(target_channel_id=999, message_text="Deploy"),
                guild_id=0,
                request_channel_id=10,
                user_id=20,
                source_message_id=30,
            )

    async def test_proposals_and_capabilities_expire(self) -> None:
        proposal = await self._proposal()
        self.now += timedelta(minutes=6)
        with self.assertRaisesRegex(ActionConfirmationError, "missing, expired"):
            await self.store.confirm(
                proposal.proposal_id,
                guild_id=1,
                channel_id=10,
                user_id=20,
            )

        proposal = await self._proposal()
        capability = await self.store.confirm(
            proposal.proposal_id,
            guild_id=1,
            channel_id=10,
            user_id=20,
        )
        self.now += timedelta(seconds=31)
        with self.assertRaisesRegex(ActionConfirmationError, "invalid, expired"):
            await self.store.consume(
                capability.token,
                guild_id=1,
                channel_id=10,
                user_id=20,
            )

    async def test_server_rendered_cards_bind_exact_payload_and_neutralize_mentions(self) -> None:
        send_proposal = await self._proposal()
        send_proposal = type(send_proposal)(
            proposal_id=send_proposal.proposal_id,
            kind=send_proposal.kind,
            payload=ChannelMessageAction(
                target_channel_id=999,
                message_text="Deploy\n@everyone now",
            ),
            guild_id=send_proposal.guild_id,
            request_channel_id=send_proposal.request_channel_id,
            user_id=send_proposal.user_id,
            source_message_id=send_proposal.source_message_id,
            created_at=send_proposal.created_at,
            expires_at=send_proposal.expires_at,
        )
        reminder_proposal = await self.store.propose(
            kind=ActionKind.CREATE_REMINDER,
            payload=ReminderAction(
                reminder_text="Review launch",
                remind_at_utc=self.now + timedelta(hours=1),
                timezone_name="UTC",
                local_remind_at_text="2026-07-10 19:00:00 UTC",
                assumed_time=False,
            ),
            guild_id=1,
            request_channel_id=10,
            user_id=20,
            source_message_id=31,
        )

        send_card = render_action_proposal_card(send_proposal)
        reminder_card = render_action_proposal_card(reminder_proposal)

        self.assertIn(send_proposal.proposal_id, send_card)
        self.assertIn("`999`", send_card)
        self.assertIn('"Deploy\\n@\\u200beveryone now"', send_card)
        self.assertNotIn("@everyone", send_card)
        self.assertIn(reminder_proposal.proposal_id, reminder_card)
        self.assertIn("2026-07-10 19:00:00 UTC", reminder_card)
        self.assertIn('"Review launch"', reminder_card)

    async def test_server_rendered_card_escapes_bidi_and_invisible_controls(self) -> None:
        proposal = await self.store.propose(
            kind=ActionKind.SEND_CHANNEL_MESSAGE,
            payload=ChannelMessageAction(
                target_channel_id=999,
                message_text="approve $100\u202eUSD\u2066\u200b",
            ),
            guild_id=1,
            request_channel_id=10,
            user_id=20,
            source_message_id=30,
        )

        card = render_action_proposal_card(proposal)

        self.assertIn(r"\u202e", card)
        self.assertIn(r"\u2066", card)
        self.assertIn(r"\u200b", card)
        self.assertNotIn("\u202e", card)
        self.assertNotIn("\u2066", card)
        self.assertNotIn("\u200b", card)

    async def test_authoritative_card_is_appended_when_model_omits_it(self) -> None:
        proposal = await self._proposal()
        card = render_action_proposal_card(proposal)
        outcome = ToolOutcome(
            call_id="call-1",
            tool_name="send_msg",
            arguments="{}",
            status=ToolStatus.OK,
            content=card,
            metrics={"action_proposal_count": 1},
        )

        delivered = append_authoritative_action_cards(
            "The model incorrectly said the action was already done.",
            [outcome],
        )

        self.assertIn("Server-validated pending action", delivered)
        self.assertIn(card, delivered)
        self.assertIn(proposal.proposal_id, delivered)


if __name__ == "__main__":
    unittest.main()
