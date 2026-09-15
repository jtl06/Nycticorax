import asyncio
import gc
import unittest
from unittest.mock import Mock
import weakref

from nycti.background_worker import BoundedBackgroundWorker


class _Job:
    pass


class BackgroundWorkerRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_jobs_are_released_while_worker_waits(self):
        for fail in (False, True):
            with self.subTest(handler_fails=fail):
                async def handle(job):
                    if fail:
                        raise RuntimeError("synthetic handler failure")

                worker = BoundedBackgroundWorker(handler=handle, name="test", maxsize=1, logger=Mock())
                try:
                    job = _Job()
                    reference = weakref.ref(job)
                    self.assertTrue(worker.submit(job))
                    del job
                    await asyncio.wait_for(worker.join(), timeout=1)
                    gc.collect()
                    self.assertEqual(0, worker.pending_count)
                    self.assertFalse(worker.active)
                    self.assertFalse(worker.task.done())
                    self.assertIsNone(reference(), "Idle worker retained its completed job")
                finally:
                    await worker.close()

    async def test_cancellation_finishes_active_and_queued_job_bookkeeping(self):
        entered = asyncio.Event()

        async def handle(job):
            entered.set()
            await asyncio.Event().wait()

        worker = BoundedBackgroundWorker(handler=handle, name="test", maxsize=1, logger=Mock())
        try:
            self.assertTrue(worker.submit(_Job()))
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertTrue(worker.active)
            self.assertTrue(worker.submit(_Job()))
            await worker.close()
            await asyncio.wait_for(worker.join(), timeout=1)
            self.assertEqual(0, worker.pending_count)
            self.assertFalse(worker.active)
            self.assertFalse(worker.submit(_Job()))
        finally:
            await worker.close()
