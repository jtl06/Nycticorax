from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import hashlib
import time
from typing import Any

from nycti.background_worker import BoundedBackgroundWorker
from nycti.discord.emoji_pool import DAY, maintain_emoji_pool, popularity
from nycti.emoji_catalog import EMOJI_TOKEN, MAX_OBSERVED, ObservedEmoji, parse_emoji
from nycti.emoji_meaning import assess_emoji, safe_meaning, safe_usage_text

LOGGER = logging.getLogger(__name__)
ASSESSMENTS_PER_DAY = 4


def emoji_use_key(kind: str, message_id: int, user_id: int) -> str:
    return hashlib.sha256(f"{kind}:{message_id}:{user_id if kind == 'reaction' else ''}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class EmojiUse:
    guild_id: int
    message_id: int
    user_id: int
    emojis: tuple[ObservedEmoji, ...]
    context: str
    timestamp: float
    kind: str = "message"


@dataclass
class UsageEvidence:
    # Context and participant IDs exist only in this small expiring RAM window.
    samples: deque = field(default_factory=lambda: deque(maxlen=8))

    def add(self, job: EmojiUse) -> None:
        while self.samples and job.timestamp - self.samples[0][0] > 7 * DAY:
            self.samples.popleft()
        if not any(sample[1] == job.message_id for sample in self.samples):
            self.samples.append((job.timestamp, job.message_id, job.user_id, job.context))

    def ready(self) -> bool:
        return (len(self.samples) >= 3
                and self.samples[-1][0] - self.samples[0][0] >= 60
                and len({sample[3] for sample in self.samples if sample[3]}) >= 3)

    def examples(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(sample[3] for sample in reversed(self.samples) if sample[3]))[:3]


class EmojiLearner:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.evidence: dict[tuple[int, int], UsageEvidence] = {}
        self.jobs = BoundedBackgroundWorker[EmojiUse](
            handler=self.run, name="nycti-emoji-learning", maxsize=32, logger=LOGGER,
            error_label="Emoji learning / managed pool",
        )

    def schedule(self, message: Any) -> bool:
        if (not self.bot.settings.emoji_learning_enabled or message.author.bot
                or message.guild.id != self.bot.settings.discord_guild_id
                or not self.bot.settings.openai_vision_model):
            return False
        # Learned hints are guild-wide: don't learn them from private channel conversations.
        if (not message.channel.permissions_for(message.guild.default_role).view_channel
                or (callable(getattr(message.channel, "is_private", None)) and message.channel.is_private())):
            return False
        emojis = tuple({item.id: item for match in EMOJI_TOKEN.finditer(message.content)
                        if (item := parse_emoji(match[0])) is not None}.values())[:4]
        if not emojis:
            return False
        previous = []
        for item in reversed(self.bot.cached_messages):
            if (item.channel.id == message.channel.id and not item.author.bot and item.id < message.id
                    and (message.created_at - item.created_at).total_seconds() <= 120):
                previous.append(item)
                if len(previous) == 2:
                    break
        texts = []
        for item in [*reversed(previous), message]:
            text = item.content
            for member in [item.author, *item.mentions]:
                for name in (getattr(member, "display_name", ""), getattr(member, "name", "")):
                    if name:
                        text = text.replace(name, "[member]")
            cleaned = safe_usage_text(text)
            if cleaned:
                texts.append(cleaned)
        job = EmojiUse(message.guild.id, message.id, message.author.id, emojis, " / ".join(texts)[-480:],
                       message.created_at.timestamp())
        return self.jobs.submit(job)

    async def close(self) -> None:
        await self.jobs.close()
        self.evidence.clear()

    async def schedule_reaction(self, payload: Any) -> bool:
        bot = self.bot
        if (not bot.settings.emoji_learning_enabled or not bot.settings.openai_vision_model
                or payload.guild_id != bot.settings.discord_guild_id or not payload.emoji.is_custom_emoji()
                or payload.user_id == bot.user.id):
            return False
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return False
        channel = guild.get_channel_or_thread(payload.channel_id)
        if (channel is None or not channel.permissions_for(guild.default_role).view_channel
                or (callable(getattr(channel, "is_private", None)) and channel.is_private())):
            return False
        member = payload.member or guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if member.bot:
            return False
        emoji = parse_emoji(str(payload.emoji))
        if emoji is None:
            return False
        # Count uncached reactions too; context is optional and never fetched merely
        # to count usage. A missing cache entry does not block image-safety review.
        message = next((item for item in reversed(bot.cached_messages) if item.id == payload.message_id), None)
        context = ""
        if message is not None and not message.author.bot:
            context = safe_usage_text(message.content)
            for person in [message.author, *message.mentions]:
                for name in (getattr(person, "name", ""), getattr(person, "display_name", "")):
                    if name:
                        context = context.replace(name, "[member]")
        await bot.emoji_catalog.observe(guild.id, emoji.token)
        return self.jobs.submit(EmojiUse(guild.id, payload.message_id, payload.user_id, (emoji,), context,
                                        time.time(), kind="reaction"))

    async def run(self, job: EmojiUse) -> None:
        bot = self.bot
        if (not bot.settings.emoji_learning_enabled or job.guild_id != bot.settings.discord_guild_id
                or time.time() - job.timestamp > DAY):
            return
        catalog = bot.emoji_catalog
        guild = bot.get_guild(job.guild_id)
        if guild is None:
            return
        await catalog.load(job.guild_id)
        for key, window in list(self.evidence.items()):
            if window.samples and job.timestamp - window.samples[-1][0] > 7 * DAY:
                del self.evidence[key]
        for emoji in job.emojis:
            source_id = catalog.source_id(job.guild_id, emoji.id)
            source = str(source_id)
            key = (job.guild_id, source_id)
            evidence = self.evidence.setdefault(key, UsageEvidence())
            evidence.add(job)
            while len(self.evidence) > MAX_OBSERVED:
                del self.evidence[next(iter(self.evidence))]

            def record(learning: dict) -> bool:
                usage = learning.setdefault("usage", {})
                old = usage.get(source, {})
                use_key = emoji_use_key(job.kind, job.message_id, job.user_id)
                recent = old.get("recent_uses", [])
                if (use_key in recent or use_key in old.get("history_uses", [])
                        or (job.kind == "message" and old.get("last_message", 0) >= job.message_id)):
                    return False
                usage[source] = {
                    **old,
                    "score": popularity(old, job.timestamp) + 1, "last_used": job.timestamp,
                    "messages": old.get("messages", 0) + int(job.kind == "message"),
                    "reactions": old.get("reactions", 0) + int(job.kind == "reaction"),
                    "uses": old.get("uses", old.get("messages", 0)) + 1,
                    "recent_uses": [*recent, use_key][-64:],
                    "last_message": max(old.get("last_message", 0), job.message_id) if job.kind == "message" else old.get("last_message", 0),
                    "assessed_at": old.get("assessed_at", 0), "assessed_messages": old.get("assessed_messages", 0),
                    "safety_attempt_at": old.get("safety_attempt_at", 0),
                }
                while len(usage) > MAX_OBSERVED:
                    removable = set(usage) - set(learning.get("managed", {}))
                    oldest = min(removable, key=lambda key: usage[key]["last_used"])
                    del usage[oldest]
                return True
            changed = await catalog.update_learning(job.guild_id, record)
            if not changed:
                continue
            state = await catalog.learning_state(job.guild_id)
            flags = state.get("flags", {}).get(source, {})
            if flags.get("blocked"):
                continue
            try:
                await self.try_import(guild, emoji, source_id, state)
            except Exception:
                LOGGER.exception("Emoji auto-import check failed guild=%s source=%s.", guild.id, source)
            current = state.get("meanings", {}).get(source, {})
            if not evidence.ready() or flags.get("meaning_disabled") or current.get("manual"):
                continue
            if not await self.reserve_assessment(job.guild_id, source, time.time()):
                continue
            result = await assess_emoji(bot, guild_id=job.guild_id, emoji=emoji, examples=evidence.examples())

            def store(learning: dict) -> None:
                # An admin edit made while the model was running always wins.
                latest_flags = learning.get("flags", {}).get(source, {})
                meanings = learning.setdefault("meanings", {})
                if (latest_flags.get("blocked") or latest_flags.get("meaning_disabled")
                        or meanings.get(source, {}).get("manual")):
                    return
                if result is None:
                    meanings.pop(source, None)
                else:
                    meanings[source] = {**result, "updated_at": time.time(), "qualified": True}
                while len(meanings) > MAX_OBSERVED:
                    oldest = min(meanings, key=lambda key: meanings[key].get("updated_at", 0))
                    del meanings[oldest]
            await catalog.update_learning(job.guild_id, store)

    async def try_import(self, guild: Any, emoji: ObservedEmoji, source_id: int, state: dict) -> None:
        catalog = self.bot.emoji_catalog
        source = str(source_id)
        usage = state.get("usage", {}).get(source, {})
        local_ids = {item.id for item in guild.emojis}
        if (self.bot.settings.emoji_auto_import_limit == 0 or usage.get("uses", usage.get("messages", 0)) < 2
                or source in state.get("managed", {}) or catalog.imported_id(guild.id, source_id) in local_ids
                or source_id in local_ids or any(item.name.casefold() == emoji.name.casefold() for item in guild.emojis)):
            return
        now = time.time()
        safety = state.get("image_safety", {}).get(source, {})
        if safety.get("approved") is not True or now - safety.get("checked_at", 0) > 7 * DAY:
            if not await self.reserve_assessment(guild.id, source, now, safety_only=True):
                return
            result = await assess_emoji(self.bot, guild_id=guild.id, emoji=emoji, examples=(), safety_only=True)
            approved = result is not None and result.get("approved") is True

            def store(learning: dict) -> None:
                safety_checks = learning.setdefault("image_safety", {})
                safety_checks[source] = {"approved": approved, "checked_at": now}
                while len(safety_checks) > MAX_OBSERVED:
                    del safety_checks[min(safety_checks, key=lambda key: safety_checks[key]["checked_at"])]
            await catalog.update_learning(guild.id, store)
            LOGGER.info("Emoji image safety guild=%s source=%s approved=%s uses=%s.",
                        guild.id, source, approved, usage.get("uses", usage.get("messages", 0)))
            if not approved:
                return
        original = catalog._states[guild.id]["observed"].get(source)
        candidate = ObservedEmoji(**original) if original else emoji
        await maintain_emoji_pool(self.bot, guild, candidate, now=time.time())

    async def reserve_assessment(self, guild_id: int, source: str, now: float, *, manual: bool = False,
                                 safety_only: bool = False) -> bool:
        def reserve(learning: dict) -> bool:
            daily = learning.setdefault("daily", {})
            day = int(now // DAY)
            if daily.get("day") != day:
                daily.update(day=day, assessments=0)
            usage = learning.setdefault("usage", {}).get(source, {})
            if daily.get("assessments", 0) >= ASSESSMENTS_PER_DAY:
                return False
            uses = usage.get("uses", usage.get("messages", 0))
            if safety_only and now - usage.get("safety_attempt_at", 0) < DAY:
                return False
            if not manual and not safety_only and (now - usage.get("assessed_at", 0) < 7 * DAY
                               or uses - usage.get("assessed_messages", 0) < 3):
                return False
            daily["assessments"] = daily.get("assessments", 0) + 1
            if usage:
                if safety_only:
                    usage["safety_attempt_at"] = now
                else:
                    usage.update(assessed_at=now, assessed_messages=uses)
            return True
        return await self.bot.emoji_catalog.update_learning(guild_id, reserve)

    async def set_meaning(self, guild: Any, emoji: ObservedEmoji, meaning: str) -> str:
        if not safe_meaning(meaning):
            raise ValueError("Use a short generic meaning without personal details, links, IDs, or values.")
        source = str(self.bot.emoji_catalog.source_id(guild.id, emoji.id))
        if not await self.reserve_assessment(guild.id, source, time.time(), manual=True):
            return "Today's emoji assessment budget is used up. Try tomorrow."
        result = await assess_emoji(self.bot, guild_id=guild.id, emoji=emoji, examples=(), explicit_meaning=meaning)
        if result is None:
            return "Couldn't validate that meaning from the emoji. Nothing changed."

        def store(learning: dict) -> None:
            meanings = learning.setdefault("meanings", {})
            if source not in meanings and len(meanings) >= MAX_OBSERVED:
                raise ValueError("Emoji meaning limit reached; forget an old meaning first.")
            meanings[source] = {**result, "manual": True, "updated_at": time.time()}
            learning.setdefault("flags", {}).setdefault(source, {})["meaning_disabled"] = False
        await self.bot.emoji_catalog.update_learning(guild.id, store)
        return f"Emoji meaning override saved: {result['meaning']}"
