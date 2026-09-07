from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from nycti.background_worker import BoundedBackgroundWorker
from nycti.discord.emoji_pool import DAY, maintain_emoji_pool, popularity
from nycti.emoji_catalog import EMOJI_TOKEN, MAX_OBSERVED, ObservedEmoji, parse_emoji
from nycti.emoji_meaning import assess_emoji, safe_meaning, safe_usage_text

LOGGER = logging.getLogger(__name__)
ASSESSMENTS_PER_DAY = 4


@dataclass(frozen=True)
class EmojiUse:
    guild_id: int
    message_id: int
    user_id: int
    emojis: tuple[ObservedEmoji, ...]
    context: str
    timestamp: float


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
        return (len(self.samples) >= 5 and len({sample[2] for sample in self.samples}) >= 2
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
        if not message.channel.permissions_for(message.guild.default_role).view_channel:
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
                if old.get("last_message", 0) >= job.message_id:
                    return False
                usage[source] = {
                    "score": popularity(old, job.timestamp) + 1, "last_used": job.timestamp,
                    "messages": old.get("messages", 0) + 1, "last_message": job.message_id,
                    "assessed_at": old.get("assessed_at", 0), "assessed_messages": old.get("assessed_messages", 0),
                }
                while len(usage) > MAX_OBSERVED:
                    removable = set(usage) - set(learning.get("managed", {}))
                    oldest = min(removable, key=lambda key: usage[key]["last_used"])
                    del usage[oldest]
                return True
            changed = await catalog.update_learning(job.guild_id, record)
            if not changed or not evidence.ready():
                continue
            state = await catalog.learning_state(job.guild_id)
            flags = state.get("flags", {}).get(source, {})
            current = state.get("meanings", {}).get(source, {})
            if flags.get("blocked") or flags.get("meaning_disabled") or current.get("manual"):
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
            if result is not None:
                # Use the original source identity for import/ownership tracking even after a rename.
                original = catalog._states[job.guild_id]["observed"].get(source)
                candidate = ObservedEmoji(**original) if original else emoji
                await maintain_emoji_pool(bot, guild, candidate, now=time.time())

    async def reserve_assessment(self, guild_id: int, source: str, now: float, *, manual: bool = False) -> bool:
        def reserve(learning: dict) -> bool:
            daily = learning.setdefault("daily", {})
            day = int(now // DAY)
            if daily.get("day") != day:
                daily.update(day=day, assessments=0)
            usage = learning.setdefault("usage", {}).get(source, {})
            if daily.get("assessments", 0) >= ASSESSMENTS_PER_DAY:
                return False
            if not manual and (now - usage.get("assessed_at", 0) < 7 * DAY
                               or usage.get("messages", 0) - usage.get("assessed_messages", 0) < 5):
                return False
            daily["assessments"] = daily.get("assessments", 0) + 1
            if usage:
                usage.update(assessed_at=now, assessed_messages=usage.get("messages", 0))
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
