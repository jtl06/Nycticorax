from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import re
from typing import Any

from nycti.db.models import AppState

EMOJI_TOKEN = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):([0-9]{1,20})>")
EMOJI_NAME = re.compile(r"[A-Za-z0-9_]{2,32}")
MAX_OBSERVED = 200
MAX_OVERRIDES = 100


@dataclass(frozen=True)
class ObservedEmoji:
    id: int
    name: str
    animated: bool = False

    @property
    def token(self) -> str:
        return f"<{'a' if self.animated else ''}:{self.name}:{self.id}>"


def parse_emoji(value: str) -> ObservedEmoji | None:
    match = EMOJI_TOKEN.fullmatch(value.strip())
    if match is None or int(match[3]) <= 0:
        return None
    return ObservedEmoji(int(match[3]), match[2], bool(match[1]))


def emoji_name(value: str) -> str:
    name = value.strip().strip(":")
    if EMOJI_NAME.fullmatch(name) is None:
        raise ValueError("Emoji names must contain 2-32 letters, digits, or underscores.")
    return name


def guild_replacements(guild: Any) -> dict[str, str]:
    return {
        str(emoji.name).casefold(): str(emoji)
        for emoji in guild.emojis
        if getattr(emoji, "available", True)
        and (not callable(getattr(emoji, "is_usable", None)) or emoji.is_usable())
    }


class EmojiCatalog:
    """Bounded guild configuration, not message history or inferred semantic memory."""

    def __init__(self, database: Any) -> None:
        self.database = database
        self._states: dict[int, dict] = {}
        self._lock = asyncio.Lock()

    async def _load(self, guild_id: int) -> dict:
        if guild_id not in self._states:
            async with self.database.session() as session:
                row = await session.get(AppState, f"emoji_catalog:{guild_id}")
                self._states[guild_id] = json.loads(row.value) if row else {
                    "observed": {}, "overrides": {}, "imports": {},
                }
        return self._states[guild_id]

    async def load(self, guild_id: int) -> None:
        async with self._lock:
            await self._load(guild_id)

    async def _save(self, guild_id: int, state: dict) -> None:
        async with self.database.session() as session:
            key = f"emoji_catalog:{guild_id}"
            row = await session.get(AppState, key)
            value = json.dumps(state)
            if row is None:
                session.add(AppState(key=key, value=value))
            else:
                row.value = value
            await session.commit()
        self._states[guild_id] = state

    async def observe(self, guild_id: int, content: str) -> None:
        tokens = [parse_emoji(match[0]) for match in EMOJI_TOKEN.finditer(content)]
        tokens = [emoji for emoji in tokens if emoji is not None][:20]
        if not tokens:
            return
        async with self._lock:
            state = await self._load(guild_id)
            observed = dict(state["observed"])
            for emoji in tokens:
                # First-seen identity cannot be renamed by another user's pasted token.
                observed.setdefault(str(emoji.id), asdict(emoji))
            pinned = set(state["overrides"].values()) | set(state["imports"])
            for key in list(observed):
                if len(observed) <= MAX_OBSERVED:
                    break
                if key not in pinned:
                    del observed[key]
            if observed != state["observed"]:
                await self._save(guild_id, {**state, "observed": observed})

    def replacements(self, guild: Any) -> dict[str, str]:
        replacements = guild_replacements(guild)
        by_id = {str(parse_emoji(token).id): token for token in replacements.values()}
        state = self._states.get(guild.id, {})
        imports = state.get("imports", {})
        for source_id, item in state.get("observed", {}).items():
            token = by_id.get(imports.get(source_id, source_id))
            if token:
                replacements.setdefault(item["name"].casefold(), token)
        for alias, source_id in state.get("overrides", {}).items():
            token = by_id.get(imports.get(source_id, source_id))
            if token:
                replacements[alias] = token
            else:
                replacements.pop(alias, None)
        return replacements

    async def resolve(self, guild: Any, value: str) -> ObservedEmoji:
        await self.load(guild.id)
        parsed = parse_emoji(value)
        if parsed:
            return parsed
        state = self._states[guild.id]
        token = self.replacements(guild).get(value.strip(":").casefold())
        if token:
            return parse_emoji(token)
        override_id = state["overrides"].get(value.strip(":").casefold())
        if override_id in state["observed"]:
            return ObservedEmoji(**state["observed"][override_id])
        matches = [ObservedEmoji(**item) for item in state["observed"].values()
                   if str(item["id"]) == value or item["name"].casefold() == value.strip(":").casefold()]
        if len(matches) == 1:
            return matches[0]
        raise ValueError("Paste the exact custom emoji; that name is unknown or ambiguous.")

    async def override(self, guild_id: int, alias: str, emoji: ObservedEmoji | None) -> None:
        alias = emoji_name(alias).casefold()
        async with self._lock:
            state = await self._load(guild_id)
            overrides = dict(state["overrides"])
            if emoji is None:
                overrides.pop(alias, None)
            else:
                if alias not in overrides and len(overrides) >= MAX_OVERRIDES:
                    raise ValueError("Emoji override limit reached; delete an old override first.")
                overrides[alias] = str(emoji.id)
            await self._save(guild_id, {**state, "overrides": overrides})

    async def record_import(self, guild_id: int, source_id: int, target_id: int) -> None:
        async with self._lock:
            state = await self._load(guild_id)
            imports = dict(state["imports"])
            imports[str(source_id)] = str(target_id)
            if len(imports) > MAX_OBSERVED:
                del imports[next(iter(imports))]
            await self._save(guild_id, {**state, "imports": imports})

    def imported_id(self, guild_id: int, source_id: int) -> int | None:
        value = self._states.get(guild_id, {}).get("imports", {}).get(str(source_id))
        return int(value) if value else None

    def prompt(self, guild: Any, reference_text: str) -> str:
        replacements = self.replacements(guild)
        names = sorted(replacements, key=lambda name: (name not in reference_text.casefold(), name))[:24]
        if not names:
            return ""
        return ("\nAvailable server emoji aliases: " + ", ".join(f":{name}:" for name in names)
                + ". Use at most one when fitting; include both colons, outside backticks. "
                "Names identify emojis, not verified meanings. Do not invent IDs.")

    async def listing(self, guild: Any, page: int) -> str:
        await self.load(guild.id)
        state = self._states[guild.id]
        replacements = self.replacements(guild)
        entries = {f":{name}: {token}" for name, token in replacements.items()}
        local_ids = {str(parse_emoji(token).id) for token in replacements.values()}
        for item in state["observed"].values():
            emoji = ObservedEmoji(**item)
            if state["imports"].get(str(emoji.id), str(emoji.id)) not in local_ids:
                entries.add(f"{emoji.name} (ID {emoji.id}, needs import)")
        for alias, source in state["overrides"].items():
            if alias not in replacements:
                entries.add(f":{alias}: -> ID {source} (unavailable; import or change override)")
        lines = sorted(entries)
        pages = max(1, (len(lines) + 11) // 12)
        if not 1 <= page <= pages:
            raise ValueError(f"Page must be between 1 and {pages}.")
        return f"Emoji catalog ({page}/{pages})\n" + ("\n".join(lines[(page-1)*12:page*12]) or "No emojis learned yet.")
