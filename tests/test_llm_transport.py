import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nycti.llm.transport import OpenAISDKTransport, transport_timing


class TransportTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_keep_separate_timing_without_admission_limit(self):
        transport = OpenAISDKTransport()
        entered = 0
        all_entered = asyncio.Event()

        async def create(**request):
            nonlocal entered
            entered += 1
            if entered == 10:
                all_entered.set()
            await all_entered.wait()
            return request["model"]

        client = SimpleNamespace(responses=SimpleNamespace(create=create))

        async def invoke(index):
            result = await transport.create_response(
                client=client, request={"model": str(index)},
                timeout_seconds=None, max_retries=None,
            )
            return result, transport_timing(transport).request_ms

        results = await asyncio.wait_for(
            asyncio.gather(*(invoke(index) for index in range(10))), timeout=1,
        )
        self.assertEqual([str(index) for index in range(10)], [row[0] for row in results])
        self.assertTrue(all(row[1] >= 0 for row in results))
        self.assertEqual(0, transport_timing(transport).request_ms)

    async def test_failed_request_records_elapsed_time(self):
        transport = OpenAISDKTransport()

        async def create(**request):
            raise RuntimeError("provider failed")

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch("nycti.llm.transport.time.perf_counter", side_effect=[10.0, 10.125]):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                await transport.create_response(
                    client=client, request={}, timeout_seconds=None, max_retries=None,
                )
        self.assertEqual(125, transport_timing(transport).request_ms)
