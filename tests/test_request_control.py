import asyncio
import unittest

from nycti.request_control import ActiveRequestRegistry


class ActiveRequestRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_clear_request(self) -> None:
        registry = ActiveRequestRegistry()
        gate = asyncio.Event()
        key = (101, 202)

        async def worker() -> str:
            await gate.wait()
            return "ok"

        task = registry.start(key, worker())
        self.assertTrue(registry.has_active(key))
        gate.set()
        self.assertEqual(await task, "ok")
        registry.clear(key, task)
        self.assertFalse(registry.has_active(key))

    async def test_start_rejects_second_active_request_for_same_key(self) -> None:
        registry = ActiveRequestRegistry()
        gate = asyncio.Event()
        key = (111, 222)

        async def worker() -> str:
            await gate.wait()
            return "ok"

        task = registry.start(key, worker())
        duplicate = worker()
        try:
            with self.assertRaises(RuntimeError):
                registry.start(key, duplicate)
        finally:
            duplicate.close()
        registry.cancel(key)
        with self.assertRaises(asyncio.CancelledError):
            await task
        registry.clear(key, task)

    async def test_cancel_active_request(self) -> None:
        registry = ActiveRequestRegistry()
        gate = asyncio.Event()
        key = (123, 456)

        async def worker() -> str:
            await gate.wait()
            return "ok"

        task = registry.start(key, worker())
        self.assertTrue(registry.cancel(key))
        with self.assertRaises(asyncio.CancelledError):
            await task
        registry.clear(key, task)
        self.assertFalse(registry.cancel(key))

    async def test_cancel_all_active_requests(self) -> None:
        registry = ActiveRequestRegistry()
        gate = asyncio.Event()
        key_one = (1, 1)
        key_two = (2, 2)

        async def worker() -> str:
            await gate.wait()
            return "ok"

        task_one = registry.start(key_one, worker())
        task_two = registry.start(key_two, worker())
        self.assertEqual(registry.cancel_all(), 2)
        with self.assertRaises(asyncio.CancelledError):
            await task_one
        with self.assertRaises(asyncio.CancelledError):
            await task_two
        registry.clear(key_one, task_one)
        registry.clear(key_two, task_two)
        self.assertEqual(registry.cancel_all(), 0)


if __name__ == "__main__":
    unittest.main()
