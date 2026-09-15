"""Own short context transactions; never hold a connection during embedding IO."""
import time

from nycti.chat.context import (
    select_related_memory_user_ids,
    should_retrieve_personal_memories_for_prompt,
)
from nycti.timing import elapsed_ms


async def prepare_chat_context(builder, database, **kwargs):
    metrics = kwargs.get("timing_metrics")
    embedding_result, embedding_ms = None, 0
    service = getattr(builder, "memory_service", None)
    generator = getattr(service, "generate_retrieval_query_embedding", None)
    if kwargs["include_memories"] and callable(generator):
        user_id, guild_id = kwargs["user_id"], kwargs["guild_id"]
        async with database.session() as session:
            enabled = await service.is_enabled(session, user_id)
            aliases, identities = [], []
            if guild_id is not None:
                aliases, identities = await builder.member_alias_service.list_matching_references(
                    session, guild_id=guild_id, text=kwargs["prompt"] + "\n" + kwargs["context_text"],
                )
            related = select_related_memory_user_ids(
                current_user_id=user_id, mentioned_user_ids=kwargs.get("mentioned_user_ids", ()),
                member_aliases=aliases, member_identities=identities,
            )
            related = await service.get_enabled_user_ids(session, user_ids=related) if related else ()
            relevant = (enabled and should_retrieve_personal_memories_for_prompt(
                prompt=kwargs["prompt"], context_text=kwargs["context_text"],
            )) or bool(related)
            await session.commit()
        if relevant:
            started = time.perf_counter()
            embedding_result = await generator(query=kwargs["prompt"])
            embedding_ms = elapsed_ms(started)
    # A new session reloads opt-in, profiles and visibility after external work.
    # An explicit (None, 0) prevents fallback embedding IO inside this transaction.
    async with database.session() as session:
        prepared = await builder.prepare(session, **kwargs,
                                         prefetched_embedding=(embedding_result, embedding_ms))
        started = time.perf_counter()
        await session.commit()
        if metrics is not None:
            metrics["chat_commit_ms"] = elapsed_ms(started)
            metrics["memory_retrieval_ms"] = prepared.memory_retrieval_ms + embedding_ms
    return prepared
