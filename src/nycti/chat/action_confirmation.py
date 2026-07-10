from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import secrets
import unicodedata
from collections.abc import Iterable
from typing import Any, Callable


class ActionKind(StrEnum):
    CREATE_REMINDER = "create_reminder"
    SEND_CHANNEL_MESSAGE = "send_channel_message"


@dataclass(frozen=True, slots=True)
class ReminderAction:
    reminder_text: str
    remind_at_utc: datetime
    timezone_name: str
    local_remind_at_text: str
    assumed_time: bool


@dataclass(frozen=True, slots=True)
class ChannelMessageAction:
    target_channel_id: int
    message_text: str


ActionPayload = ReminderAction | ChannelMessageAction


@dataclass(frozen=True, slots=True)
class ActionProposal:
    proposal_id: str
    kind: ActionKind
    payload: ActionPayload
    guild_id: int
    request_channel_id: int
    user_id: int
    source_message_id: int | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActionCapability:
    token: str
    proposal: ActionProposal
    confirmed_at: datetime
    expires_at: datetime


class ActionConfirmationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionConfirmationStore:
    """In-memory, fail-closed proposal and single-use capability store.

    ``max_pending`` is enforced independently for each ``(guild, user)``
    principal.  Reaching the limit rejects the new proposal instead of
    evicting another user's still-visible confirmation card.
    """

    def __init__(
        self,
        *,
        proposal_ttl: timedelta = timedelta(minutes=5),
        capability_ttl: timedelta = timedelta(seconds=30),
        max_pending: int = 8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if proposal_ttl.total_seconds() <= 0 or capability_ttl.total_seconds() <= 0:
            raise ValueError("Action confirmation TTLs must be positive.")
        if max_pending < 1:
            raise ValueError("max_pending must be positive.")
        self.proposal_ttl = proposal_ttl
        self.capability_ttl = capability_ttl
        self.max_pending = max_pending
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._proposals: dict[str, ActionProposal] = {}
        self._capabilities: dict[str, ActionCapability] = {}
        self._lock = asyncio.Lock()

    async def propose(
        self,
        *,
        kind: ActionKind,
        payload: ActionPayload,
        guild_id: int,
        request_channel_id: int,
        user_id: int,
        source_message_id: int | None,
    ) -> ActionProposal:
        async with self._lock:
            now = self._utc_now()
            self._prune(now)
            self._validate_proposal(
                kind=kind,
                payload=payload,
                guild_id=guild_id,
                request_channel_id=request_channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                now=now,
            )
            principal = (guild_id, user_id)
            pending_for_principal = sum(
                (proposal.guild_id, proposal.user_id) == principal
                for proposal in self._proposals.values()
            )
            if pending_for_principal >= self.max_pending:
                raise ActionConfirmationError(
                    "too_many_pending",
                    "You already have the maximum number of pending actions in this server. "
                    "Confirm one or wait for it to expire before proposing another.",
                )
            proposal = ActionProposal(
                proposal_id=self._new_token("act"),
                kind=kind,
                payload=payload,
                guild_id=guild_id,
                request_channel_id=request_channel_id,
                user_id=user_id,
                source_message_id=source_message_id,
                created_at=now,
                expires_at=now + self.proposal_ttl,
            )
            self._proposals[proposal.proposal_id] = proposal
            return proposal

    async def confirm(
        self,
        proposal_id: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> ActionCapability:
        """Confirm an exact proposal and mint an internal short-lived capability."""
        async with self._lock:
            now = self._utc_now()
            self._prune(now)
            proposal = self._proposals.get(proposal_id.strip())
            if proposal is None:
                raise ActionConfirmationError(
                    "missing_or_expired",
                    "That action proposal is missing, expired, or already confirmed.",
                )
            self._validate_identity(
                proposal,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            del self._proposals[proposal.proposal_id]
            capability = ActionCapability(
                token=self._new_token("cap"),
                proposal=proposal,
                confirmed_at=now,
                expires_at=now + self.capability_ttl,
            )
            self._capabilities[capability.token] = capability
            return capability

    async def consume(
        self,
        token: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> ActionProposal:
        """Consume a capability once, rechecking every identity binding."""
        async with self._lock:
            now = self._utc_now()
            self._prune(now)
            capability = self._capabilities.get(token)
            if capability is None:
                raise ActionConfirmationError(
                    "invalid_capability",
                    "The action capability is invalid, expired, or already used.",
                )
            self._validate_identity(
                capability.proposal,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            del self._capabilities[token]
            return capability.proposal

    async def pending_count(self) -> int:
        async with self._lock:
            self._prune(self._utc_now())
            return len(self._proposals)

    def _validate_identity(
        self,
        proposal: ActionProposal,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> None:
        if proposal.user_id != user_id:
            raise ActionConfirmationError(
                "wrong_user",
                "Only the user who requested this action can confirm it.",
            )
        if proposal.guild_id != guild_id or proposal.request_channel_id != channel_id:
            raise ActionConfirmationError(
                "wrong_context",
                "Confirm this action in the same server and channel where it was proposed.",
            )

    @staticmethod
    def _validate_proposal(
        *,
        kind: ActionKind,
        payload: ActionPayload,
        guild_id: int,
        request_channel_id: int,
        user_id: int,
        source_message_id: int | None,
        now: datetime,
    ) -> None:
        identifiers = (guild_id, request_channel_id, user_id)
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in identifiers):
            raise ActionConfirmationError(
                "invalid_proposal",
                "Action proposal identity bindings must be positive Discord IDs.",
            )
        if source_message_id is not None and (
            not isinstance(source_message_id, int)
            or isinstance(source_message_id, bool)
            or source_message_id <= 0
        ):
            raise ActionConfirmationError(
                "invalid_proposal",
                "The source message binding must be a positive Discord ID.",
            )
        if kind == ActionKind.CREATE_REMINDER:
            if not isinstance(payload, ReminderAction):
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Reminder proposals require an exact reminder payload.",
                )
            if not payload.reminder_text.strip() or not payload.timezone_name.strip():
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Reminder proposals require non-empty text and timezone bindings.",
                )
            remind_at = payload.remind_at_utc
            if remind_at.tzinfo is None or remind_at.utcoffset() is None:
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Reminder proposal times must include a timezone offset.",
                )
            if remind_at.astimezone(timezone.utc) <= now:
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Reminder proposal times must be in the future.",
                )
            return
        if kind == ActionKind.SEND_CHANNEL_MESSAGE:
            if not isinstance(payload, ChannelMessageAction):
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Channel-send proposals require an exact channel-message payload.",
                )
            if (
                not isinstance(payload.target_channel_id, int)
                or isinstance(payload.target_channel_id, bool)
                or payload.target_channel_id <= 0
                or not payload.message_text.strip()
            ):
                raise ActionConfirmationError(
                    "invalid_proposal",
                    "Channel-send proposals require a positive target ID and non-empty message.",
                )
            return
        raise ActionConfirmationError("invalid_proposal", "Unsupported action proposal kind.")

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _prune(self, now: datetime) -> None:
        self._proposals = {
            key: value
            for key, value in self._proposals.items()
            if value.expires_at > now
        }
        self._capabilities = {
            key: value
            for key, value in self._capabilities.items()
            if value.expires_at > now
        }

    @staticmethod
    def _new_token(prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(9)}"


def render_action_proposal_card(proposal: ActionProposal) -> str:
    """Render the authoritative proposal without relying on model wording.

    Message text is represented as a JSON string so whitespace and newlines
    remain unambiguous.  Discord mention markers are neutralized in the
    preview; confirmed delivery applies its own mention policy separately.
    """

    expires_at = int(proposal.expires_at.timestamp())
    lines = [
        "Confirmation required",
        f"Proposal: `{proposal.proposal_id}`",
    ]
    if proposal.kind == ActionKind.CREATE_REMINDER:
        payload = proposal.payload
        if not isinstance(payload, ReminderAction):
            raise TypeError("Reminder proposal payload type mismatch.")
        lines.extend(
            (
                "Action: create reminder",
                f"When: {payload.local_remind_at_text}",
                f"Text: {_safe_preview_literal(payload.reminder_text)}",
            )
        )
    elif proposal.kind == ActionKind.SEND_CHANNEL_MESSAGE:
        payload = proposal.payload
        if not isinstance(payload, ChannelMessageAction):
            raise TypeError("Channel-message proposal payload type mismatch.")
        lines.extend(
            (
                "Action: send channel message",
                f"Target: <#{payload.target_channel_id}> (`{payload.target_channel_id}`)",
                f"Message: {_safe_preview_literal(payload.message_text)}",
            )
        )
    else:  # pragma: no cover - exhaustive defensive guard
        raise TypeError(f"Unsupported action proposal kind: {proposal.kind}")
    lines.extend(
        (
            "Nothing has been executed.",
            f"Expires: <t:{expires_at}:R>",
            f"Confirm in this channel with `/confirm proposal:{proposal.proposal_id}`.",
        )
    )
    return "\n".join(lines)


def append_authoritative_action_cards(text: str, outcomes: Iterable[Any]) -> str:
    """Append successful server-rendered cards even if model synthesis omitted them."""

    cards: list[str] = []
    for outcome in outcomes:
        metrics = getattr(outcome, "metrics", {})
        content = str(getattr(outcome, "content", "")).strip()
        status = str(getattr(outcome, "status", ""))
        if (
            isinstance(metrics, dict)
            and int(metrics.get("action_proposal_count", 0)) > 0
            and status == "ok"
            and content.startswith("Confirmation required\n")
        ):
            cards.append(content)
    if not cards:
        return text
    authoritative = "\n\n".join(dict.fromkeys(cards))
    prefix = text.rstrip()
    if prefix:
        return (
            f"Server-validated pending action:\n{authoritative}\n\n"
            f"Additional model commentary (non-authoritative):\n{prefix}"
        )
    return f"Server-validated pending action:\n{authoritative}"


def _safe_preview_literal(value: str) -> str:
    rendered = "".join(
        _escape_preview_character(character)
        for character in json.dumps(value, ensure_ascii=False)
    ).replace("@", "@\\u200b")
    for marker in "`*_~|[]<>":
        rendered = rendered.replace(marker, f"\\{marker}")
    return rendered


def _escape_preview_character(character: str) -> str:
    category = unicodedata.category(character)
    if not category.startswith("C") and category not in {"Zl", "Zp"}:
        return character
    codepoint = ord(character)
    return (
        f"\\u{codepoint:04x}"
        if codepoint <= 0xFFFF
        else f"\\U{codepoint:08x}"
    )
