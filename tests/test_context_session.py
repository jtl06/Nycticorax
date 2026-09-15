import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from nycti.channel_aliases import ChannelAliasService
from nycti.chat.context import ChatContextBuilder
from nycti.chat.context_session import prepare_chat_context
from nycti.config import Settings
from nycti.db.models import UserSettings
from nycti.db.session import Database
from nycti.member_aliases import MemberAliasService
from nycti.memory.retriever import MemoryRetriever
from nycti.memory.service import MemoryService


class ContextSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_wait_releases_connection_and_rechecks_opt_in(self):
        await self.exercise(cancel=False)

    async def test_cancelled_embedding_leaves_no_checked_out_connection(self):
        await self.exercise(cancel=True)

    async def exercise(self, *, cancel):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(discord_token="test", openai_api_key="test",
                database_url=f"sqlite:///{Path(directory)/'context.db'}")
            database = Database(settings)
            started, release = asyncio.Event(), asyncio.Event()
            service = MemoryService(extractor=SimpleNamespace(), retriever=MemoryRetriever(settings),
                llm_client=SimpleNamespace(), embedding_model=None)

            async def embedding(**_kwargs):
                self.assertEqual(0, database.engine.pool.checkedout())
                started.set()
                await release.wait()
                return None

            service.generate_retrieval_query_embedding = embedding
            builder = ChatContextBuilder(memory_service=service, channel_alias_service=ChannelAliasService(),
                member_alias_service=MemberAliasService())
            task = None
            try:
                await database.init_models()
                async with database.session() as session:
                    session.add(UserSettings(user_id=1, memory_enabled=True, personal_profile_md="fixture private keyboard preference"))
                    await session.commit()
                task = asyncio.create_task(prepare_chat_context(builder, database,
                    guild_id=None, user_id=1, prompt="what keyboard should I get for my setup?",
                    context_text="", include_memories=True))
                await asyncio.wait_for(started.wait(), 2)
                # This transaction must complete while the external provider is still blocked.
                async def opt_out():
                    async with database.session() as session:
                        await service.set_enabled(session, 1, False)
                        await session.commit()
                await asyncio.wait_for(opt_out(), 2)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    release.set()
                    result = await asyncio.wait_for(task, 2)
                    self.assertFalse(result.memory_enabled)
                    self.assertNotIn("fixture private", result.personal_profile_block)
                    self.assertEqual([], result.retrieved_memories)
                self.assertEqual(0, database.engine.pool.checkedout())
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await database.engine.dispose()
