from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from nycti.discord.emoji_pool import DAY, popularity
from nycti.emoji_catalog import EMOJI_TOKEN, MAX_OBSERVED, ObservedEmoji, parse_emoji
from nycti.emoji_learning import emoji_use_key

LOGGER = logging.getLogger(__name__)
BACKFILL_VERSION = 1
MAX_CHANNELS = 6
MAX_MESSAGES_PER_CHANNEL = 300
MAX_REACTION_LOOKUPS = 30
MAX_REACTORS = 10
MAX_USES_PER_EMOJI = 64


@dataclass
class HistoricalEmoji:
    emoji: ObservedEmoji
    uses: dict[str, tuple[str, float]] = field(default_factory=dict)
    last_message: int = 0

    def add(self, *, kind: str, message_id: int, user_id: int, timestamp: float) -> None:
        if kind == "message":
            self.last_message = max(self.last_message, message_id)
        key = emoji_use_key(kind, message_id, user_id)
        if len(self.uses) < MAX_USES_PER_EMOJI:
            self.uses.setdefault(key, (kind, timestamp))


@dataclass
class EmojiHistory:
    emojis: dict[int, HistoricalEmoji] = field(default_factory=dict)
    messages: int = 0
    channels: int = 0
    reaction_lookups: int = 0
    capped: bool = False
    errors: int = 0


async def collect_emoji_history(bot: Any, guild: Any, *, now: datetime, channels: Any = None) -> EmojiHistory:
    """Keep only bounded emoji metadata; no message bodies or user identities survive the scan."""
    history = EmojiHistory()
    cutoff = now - timedelta(days=7)
    me = await guild.fetch_member(bot.user.id)

    def add(emoji: ObservedEmoji, message: Any, *, kind: str, user_id: int) -> None:
        source = bot.emoji_catalog.source_id(guild.id, emoji.id)
        if source not in history.emojis:
            if len(history.emojis) >= MAX_OBSERVED:
                history.capped = True
                return
            history.emojis[source] = HistoricalEmoji(ObservedEmoji(source, emoji.name, emoji.animated))
        history.emojis[source].add(kind=kind, message_id=message.id, user_id=user_id,
                                   timestamp=message.created_at.timestamp())

    for channel in channels if channels is not None else guild.text_channels:
        if channel.guild.id != guild.id:
            continue
        permissions = channel.permissions_for(me)
        if (not channel.permissions_for(guild.default_role).view_channel or not permissions.view_channel
                or not permissions.read_message_history
                or (callable(getattr(channel, "is_private", None)) and channel.is_private())):
            continue
        if history.channels >= MAX_CHANNELS:
            history.capped = True
            break
        history.channels += 1
        channel_messages = 0
        try:
            async for message in channel.history(limit=MAX_MESSAGES_PER_CHANNEL, after=cutoff, before=now, oldest_first=False):
                if not cutoff <= message.created_at < now:
                    continue
                channel_messages += 1
                history.messages += 1
                if not message.author.bot:
                    found = {emoji.id: emoji for match in EMOJI_TOKEN.finditer(message.content)
                             if (emoji := parse_emoji(match[0])) is not None}
                    for emoji in list(found.values())[:4]:
                        add(emoji, message, kind="message", user_id=message.author.id)
                for reaction in message.reactions:
                    emoji = parse_emoji(str(reaction.emoji))
                    if emoji is None:
                        continue
                    if history.reaction_lookups >= MAX_REACTION_LOOKUPS:
                        history.capped = True
                        continue
                    history.reaction_lookups += 1
                    try:
                        # Do not trust reaction.count: it includes bots and lacks dedup identities.
                        async for user in reaction.users(limit=MAX_REACTORS):
                            if not user.bot:
                                add(emoji, message, kind="reaction", user_id=user.id)
                    except Exception:
                        history.errors += 1
                        LOGGER.warning("Emoji backfill reaction lookup failed guild=%s channel=%s.", guild.id, channel.id)
            if channel_messages >= MAX_MESSAGES_PER_CHANNEL:
                history.capped = True
        except Exception:
            history.errors += 1
            LOGGER.exception("Emoji backfill history read failed guild=%s channel=%s.", guild.id, channel.id)
    return history


async def merge_emoji_history(catalog: Any, guild_id: int, history: EmojiHistory, *, now: float) -> None:
    tokens = [entry.emoji.token for entry in history.emojis.values()]
    for index in range(0, len(tokens), 20):
        await catalog.observe(guild_id, " ".join(tokens[index:index+20]))

    def merge(state: dict) -> None:
        usage = state.setdefault("usage", {})
        for source_id, entry in history.emojis.items():
            if not entry.uses:
                continue
            source = str(source_id)
            old = usage.get(source, {})
            messages = sum(kind == "message" for kind, _ in entry.uses.values())
            reactions = len(entry.uses) - messages
            message_floor = max(old.get("messages", 0), messages)
            reaction_floor = max(old.get("reactions", 0), reactions)
            anchor = max(old.get("last_used", 0), max(ts for _, ts in entry.uses.values()))
            # Live and historical samples overlap. Floors deliberately undercount uncertain
            # unions instead of adding duplicates or replacing newer live counters.
            usage[source] = {
                **old, "messages": message_floor, "reactions": reaction_floor,
                "uses": max(old.get("uses", old.get("messages", 0)), message_floor + reaction_floor),
                "score": max(popularity(old, anchor), sum(0.5 ** (max(0, anchor-ts)/(7*DAY)) for _, ts in entry.uses.values())),
                "last_used": anchor,
                "last_message": max(old.get("last_message", 0), entry.last_message),
                "history_uses": list(entry.uses),
            }
        while len(usage) > MAX_OBSERVED:
            removable = set(usage) - set(state.get("managed", {}))
            del usage[min(removable, key=lambda key: usage[key].get("last_used", 0))]
    await catalog.update_learning(guild_id, merge)


class EmojiHistoryBackfill:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def start(self, guild: Any) -> None:
        if self.task is None or self.task.done():
            async def background() -> None:
                try:
                    await self.run(guild)
                except Exception:
                    LOGGER.exception("Could not initialize emoji backfill guild=%s.", guild.id)
            self.task = asyncio.create_task(background(), name="nycti-emoji-history-backfill")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self, guild: Any, *, force: bool = False) -> str:
        if self.lock.locked():
            return "An emoji backfill is already running."
        async with self.lock:
            settings = self.bot.settings
            if (guild.id != settings.discord_guild_id or not settings.emoji_learning_enabled
                    or not settings.openai_vision_model or not settings.emoji_auto_import_limit):
                return "Emoji backfill is disabled by the current guild/learning/import configuration."
            catalog = self.bot.emoji_catalog
            state = await catalog.learning_state(guild.id)
            previous = state.get("backfill", {})
            if previous.get("version") == BACKFILL_VERSION and previous.get("status") == "complete" and not force:
                return "The initial seven-day emoji backfill is already complete."
            now = datetime.now(timezone.utc)
            if now.timestamp() - previous.get("attempted_at", 0) < 3600:
                return "Emoji backfill is limited to once per hour."
            await catalog.update_learning(guild.id, lambda data: data.update(backfill={
                "version": BACKFILL_VERSION, "status": "running", "attempted_at": now.timestamp(),
            }))
            try:
                history = await asyncio.wait_for(collect_emoji_history(self.bot, guild, now=now), timeout=90)
                await merge_emoji_history(catalog, guild.id, history, now=now.timestamp())
                await catalog.update_learning(guild.id, lambda data: data.update(backfill={
                    "version": BACKFILL_VERSION, "status": "complete" if not history.errors else "partial",
                    "attempted_at": now.timestamp(), "messages": history.messages, "channels": history.channels,
                    "emoji_count": len(history.emojis), "capped": history.capped, "errors": history.errors,
                }))
                state = await catalog.learning_state(guild.id)
                candidates = sorted(history.emojis, key=lambda source: popularity(state.get("usage", {}).get(str(source), {}), now.timestamp()), reverse=True)
                before = sum(bool(item.get("id")) for item in state.get("managed", {}).values())
                for source in candidates[:20]:
                    state = await catalog.learning_state(guild.id)
                    if state.get("flags", {}).get(str(source), {}).get("blocked"):
                        continue
                    try:
                        await self.bot._emoji_learner.try_import(guild, history.emojis[source].emoji, source, state)
                    except Exception:
                        LOGGER.exception("Emoji backfill import check failed guild=%s source=%s.", guild.id, source)
                state = await catalog.learning_state(guild.id)
                added = sum(bool(item.get("id")) for item in state.get("managed", {}).values()) - before
                result = (f"Scanned {history.messages} recent messages in {history.channels} public channels; "
                          f"found {len(history.emojis)} emoji IDs, added {max(0, added)} auto-imports. "
                          f"Bounded scan: {history.capped}; read errors: {history.errors}. "
                          "Remaining candidates may need more uses, image approval, or daily budget.")
                LOGGER.info("Emoji backfill guild=%s: %s", guild.id, result)
                return result
            except Exception:
                LOGGER.exception("Emoji history backfill failed guild=%s.", guild.id)
                await catalog.update_learning(guild.id, lambda data: data.update(backfill={
                    "version": BACKFILL_VERSION, "status": "failed", "attempted_at": now.timestamp(),
                }))
                return "Emoji backfill failed; no history text was stored. Check the logs before retrying."
