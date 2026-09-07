from __future__ import annotations

import asyncio
import logging
from typing import Any

from nycti.chat.action_confirmation import EmojiImportAction
from nycti.discord.emoji_import import can_create_emoji, fetch_emoji_image
from nycti.emoji_catalog import ObservedEmoji

LOGGER = logging.getLogger(__name__)
DAY = 86400


def popularity(stats: dict, now: float) -> float:
    return float(stats.get("score", 0)) * 0.5 ** (max(0, now - stats.get("last_used", now)) / (7 * DAY))


def rotation_victim(state: dict, candidate: str, now: float, protected_ids: set[str]) -> str | None:
    candidate_score = popularity(state.get("usage", {}).get(candidate, {}), now)
    eligible = []
    for source, owned in state.get("managed", {}).items():
        flags = state.get("flags", {}).get(source, {})
        usage = state.get("usage", {}).get(source, {})
        if (not owned.get("id") or flags.get("pinned") or str(owned["id"]) in protected_ids
                or source in protected_ids or now - usage.get("last_used", owned["created_at"]) < 7 * DAY
                or now - owned["created_at"] < 7 * DAY):
            continue
        score = popularity(usage, now)
        if candidate_score > max(1.0, score * 2):
            eligible.append((score, source))
    return min(eligible)[1] if eligible else None


async def maintain_emoji_pool(bot: Any, guild: Any, emoji: ObservedEmoji, *, now: float) -> None:
    limit = min(20, max(0, bot.settings.emoji_auto_import_limit))
    if not limit or guild.id != bot.settings.discord_guild_id:
        return
    if not hasattr(bot, "_emoji_import_lock"):
        bot._emoji_import_lock = asyncio.Lock()
    async with bot._emoji_import_lock:
        catalog = bot.emoji_catalog
        state = await catalog.learning_state(guild.id)
        source = str(emoji.id)
        flags = state.get("flags", {}).get(source, {})
        if (flags.get("blocked") or flags.get("meaning_disabled") or source in state.get("managed", {})
                or not state.get("meanings", {}).get(source, {}).get("qualified")
                or state.get("usage", {}).get(source, {}).get("messages", 0) < 5):
            return
        me = await guild.fetch_member(bot.user.id)
        if not can_create_emoji(me):
            return
        existing = await guild.fetch_emojis()
        imported_id = catalog.imported_id(guild.id, emoji.id)
        if any(item.id in {emoji.id, imported_id} or item.name.casefold() == emoji.name.casefold() for item in existing):
            return
        owned = state.get("managed", {})
        victim = None
        if len(owned) >= limit:
            if len(owned) > limit:
                return  # Lowering the limit never causes a mass deletion.
            protected = set(catalog._states[guild.id].get("overrides", {}).values())
            victim = rotation_victim(state, source, now, protected)
            if victim is None or now - state.get("last_rotation", 0) < DAY:
                return
        old = next((item for item in existing if victim and item.id == owned[victim]["id"]), None)
        if victim and (old is None or getattr(getattr(old, "user", None), "id", None) != bot.user.id
                       or old.name != owned[victim]["name"] or getattr(old, "roles", ())):
            return  # Ledger membership AND Discord creator identity are required to delete.
        same_type_count = sum(item.animated == emoji.animated for item in existing)
        freed = int(old is not None and old.animated == emoji.animated)
        if same_type_count - freed >= guild.emoji_limit:
            return
        image = await fetch_emoji_image(EmojiImportAction(emoji.id, emoji.name, emoji.animated))
        # Re-read admin controls after network waits, then hold the catalog lock through
        # the write so a concurrent pin cannot race the deletion.
        async with catalog._lock:
            root = await catalog._load(guild.id)
            from copy import deepcopy
            latest = deepcopy(root.get("learning", {}))
            if latest.get("flags", {}).get(source, {}).get("blocked"):
                return
            if victim:
                protected = set(root.get("overrides", {}).values())
                if rotation_victim(latest, source, now, protected) != victim:
                    return
                await old.delete(reason="Nycti managed emoji pool: unused for a week; replaced by a more popular emoji")
                latest["managed"].pop(victim)
                latest["last_rotation"] = now
            # Reserve a slot durably BEFORE the non-idempotent create request. A timeout
            # leaves a visible pending slot instead of risking duplicate uploads on restart.
            latest.setdefault("managed", {})[source] = {
                "id": None, "name": emoji.name, "created_at": now, "animated": emoji.animated,
            }
            await catalog._save(guild.id, {**root, "learning": latest})
        created = await guild.create_custom_emoji(name=emoji.name, image=image, reason="Nycti popular-emoji managed pool")

        def finish(learning: dict) -> None:
            learning["managed"][source]["id"] = created.id
        await catalog.update_learning(guild.id, finish)
        await catalog.record_import(guild.id, emoji.id, created.id)
        LOGGER.info("Emoji pool imported guild=%s source=%s target=%s replaced=%s.", guild.id, emoji.id, created.id, victim)
