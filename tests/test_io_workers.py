import asyncio
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from nycti.config import ConfigurationError, Settings
from nycti.io_workers import configure_io_workers


class WorkerConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_preserves_existing_executor(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "set_default_executor") as configure:
            configure_io_workers(0)
        configure.assert_not_called()

    async def test_opt_in_pool_is_used_by_to_thread(self):
        configure_io_workers(2)
        loop = asyncio.get_running_loop()
        self.assertIsInstance(loop._default_executor, ThreadPoolExecutor)
        self.assertEqual(2, loop._default_executor._max_workers)
        self.assertEqual([1, 2, 3], await asyncio.gather(*(asyncio.to_thread(int, x) for x in (1, 2, 3))))

    def test_validated_settings_and_default(self):
        env = {"DISCORD_TOKEN": "unused", "OPENAI_API_KEY": "unused", "DATABASE_URL": "sqlite:///test.db"}
        self.assertEqual(0, Settings.from_env(env).io_max_workers)
        self.assertEqual(24, Settings.from_env({**env, "IO_MAX_WORKERS": "24"}).io_max_workers)
        for value in ("-1", "33", "oops"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Settings.from_env({**env, "IO_MAX_WORKERS": value})
