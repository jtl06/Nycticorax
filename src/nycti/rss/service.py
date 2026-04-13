from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import AppState, RSSFeedSubscription
from nycti.rss.client import RSSClient, RSSFetchError, RSSParseError
from nycti.rss.models import RSSItem

LOGGER = logging.getLogger(__name__)
MAX_SEEN_IDS = 100


@dataclass(frozen=True, slots=True)
class RSSFeedPost:
    target_key: str
    channel_id: int
    feed_url: str
    feed_title: str
    item: RSSItem


class RSSService:
    def __init__(
        self,
        *,
        client: RSSClient,
        feed_urls: tuple[str, ...],
        default_channel_id: int | None,
        post_limit_per_poll: int,
    ) -> None:
        self.client = client
        self.feed_urls = feed_urls
        self.default_channel_id = default_channel_id
        self.post_limit_per_poll = post_limit_per_poll

    async def collect_new_posts(self, session: AsyncSession) -> list[RSSFeedPost]:
        posts: list[RSSFeedPost] = []
        for target in await self._list_targets(session):
            try:
                feed = await self.client.fetch_feed(target.feed_url)
            except (RSSFetchError, RSSParseError):
                LOGGER.exception("RSS feed fetch failed for %s.", target.feed_url)
                continue
            current_ids = [item.identity for item in feed.items if item.identity]
            if not current_ids:
                continue
            if target.subscription is not None and feed.title and feed.title != target.subscription.title:
                target.subscription.title = feed.title[:255]
                await session.flush()
            seen_ids = await self._get_seen_ids(session, target_key=target.key)
            if seen_ids is None:
                await self._set_seen_ids(session, target_key=target.key, seen_ids=current_ids[:MAX_SEEN_IDS])
                continue
            new_items = [item for item in feed.items if item.identity not in seen_ids]
            for item in reversed(new_items[: self.post_limit_per_poll]):
                posts.append(
                    RSSFeedPost(
                        target_key=target.key,
                        channel_id=target.channel_id,
                        feed_url=feed.url,
                        feed_title=feed.title,
                        item=item,
                    )
                )
        return posts

    async def add_feed(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        channel_id: int,
        feed_url: str,
        created_by_id: int | None,
    ) -> RSSFeedSubscription:
        normalized_url = normalize_feed_url(feed_url)
        feed = await self.client.fetch_feed(normalized_url)
        existing = await session.scalar(
            select(RSSFeedSubscription).where(
                RSSFeedSubscription.guild_id == guild_id,
                RSSFeedSubscription.channel_id == channel_id,
                RSSFeedSubscription.feed_url == normalized_url,
            )
        )
        if existing is not None:
            existing.title = (feed.title or existing.title)[:255]
            await session.flush()
            await self._set_seen_ids(
                session,
                target_key=_subscription_target_key(existing.id),
                seen_ids=[item.identity for item in feed.items if item.identity][:MAX_SEEN_IDS],
            )
            return existing
        subscription = RSSFeedSubscription(
            guild_id=guild_id,
            channel_id=channel_id,
            feed_url=normalized_url,
            title=(feed.title or "")[:255],
            created_by_id=created_by_id,
        )
        session.add(subscription)
        await session.flush()
        await self._set_seen_ids(
            session,
            target_key=_subscription_target_key(subscription.id),
            seen_ids=[item.identity for item in feed.items if item.identity][:MAX_SEEN_IDS],
        )
        return subscription

    async def delete_feed(self, session: AsyncSession, *, guild_id: int, feed_id: int) -> bool:
        subscription = await session.get(RSSFeedSubscription, feed_id)
        if subscription is None or subscription.guild_id != guild_id:
            return False
        await self._delete_seen_ids(session, target_key=_subscription_target_key(subscription.id))
        await session.delete(subscription)
        await session.flush()
        return True

    async def list_feeds(self, session: AsyncSession, *, guild_id: int) -> list[RSSFeedSubscription]:
        stmt = (
            select(RSSFeedSubscription)
            .where(RSSFeedSubscription.guild_id == guild_id)
            .order_by(RSSFeedSubscription.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def mark_posted(self, session: AsyncSession, *, target_key: str, item_identity: str) -> None:
        seen_ids = await self._get_seen_ids(session, target_key=target_key) or []
        await self._set_seen_ids(
            session,
            target_key=target_key,
            seen_ids=[item_identity, *[seen_id for seen_id in seen_ids if seen_id != item_identity]][:MAX_SEEN_IDS],
        )

    async def _list_targets(self, session: AsyncSession) -> list["_FeedTarget"]:
        targets: list[_FeedTarget] = []
        if self.default_channel_id is not None:
            for feed_url in self.feed_urls:
                targets.append(
                    _FeedTarget(
                        key=_env_target_key(channel_id=self.default_channel_id, feed_url=feed_url),
                        channel_id=self.default_channel_id,
                        feed_url=feed_url,
                        subscription=None,
                    )
                )
        stmt = select(RSSFeedSubscription).order_by(RSSFeedSubscription.id.asc())
        for subscription in (await session.scalars(stmt)).all():
            targets.append(
                _FeedTarget(
                    key=_subscription_target_key(subscription.id),
                    channel_id=subscription.channel_id,
                    feed_url=subscription.feed_url,
                    subscription=subscription,
                )
            )
        return targets

    async def _get_seen_ids(self, session: AsyncSession, *, target_key: str) -> list[str] | None:
        state = await session.get(AppState, _seen_state_key(target_key))
        if state is None:
            return None
        try:
            payload = json.loads(state.value)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload if str(item).strip()]

    async def _set_seen_ids(self, session: AsyncSession, *, target_key: str, seen_ids: list[str]) -> None:
        key = _seen_state_key(target_key)
        value = json.dumps(seen_ids[:MAX_SEEN_IDS])
        state = await session.get(AppState, key)
        if state is None:
            session.add(AppState(key=key, value=value))
            await session.flush()
            return
        state.value = value
        await session.flush()

    async def _delete_seen_ids(self, session: AsyncSession, *, target_key: str) -> None:
        state = await session.get(AppState, _seen_state_key(target_key))
        if state is not None:
            await session.delete(state)
            await session.flush()


def format_rss_post(post: RSSFeedPost) -> str:
    title = _truncate(post.item.title, 180)
    feed_title = _truncate(post.feed_title, 80)
    lines = [f"**{title}**"]
    if post.item.link:
        lines.append(post.item.link)
    elif post.item.summary:
        lines.append(_truncate(post.item.summary, 300))
    lines.append(f"_Source: {feed_title}_")
    return "\n".join(lines)


def format_rss_feed_list(feeds: list[RSSFeedSubscription]) -> str:
    if not feeds:
        return "No RSS feeds configured for this server."
    lines = ["Configured RSS feeds:"]
    for feed in feeds:
        title = f" ({_truncate(feed.title, 80)})" if feed.title else ""
        lines.append(f"- `{feed.id}` -> <#{feed.channel_id}>: {feed.feed_url}{title}")
    return "\n".join(lines)


def normalize_feed_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.startswith(("https://", "http://")):
        raise ValueError("RSS feed URL must start with http:// or https://.")
    return cleaned


@dataclass(frozen=True, slots=True)
class _FeedTarget:
    key: str
    channel_id: int
    feed_url: str
    subscription: RSSFeedSubscription | None


def _seen_state_key(target_key: str) -> str:
    digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()[:24]
    return f"rss_seen:{digest}"


def _env_target_key(*, channel_id: int, feed_url: str) -> str:
    return f"env:{channel_id}:{feed_url}"


def _subscription_target_key(feed_id: int) -> str:
    return f"feed:{feed_id}"


def _truncate(value: str, limit: int) -> str:
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
