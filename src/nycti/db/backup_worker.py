"""Run daily S3 backups in a disposable process with bounded retries and shutdown."""
import asyncio
from contextlib import suppress
import json
import logging
import os
import sys

from sqlalchemy.engine import make_url

LOGGER = logging.getLogger(__name__)


class SQLiteBackupWorker:
    def __init__(self, settings):
        self.config = getattr(settings, "sqlite_backup", None)
        url = make_url(settings.database_url)
        self.source = url.database if url.get_backend_name() == "sqlite" else None
        self.task = None

    def start(self):
        if self.config is not None and self.source not in (None, "", ":memory:") and self.task is None:
            self.task = asyncio.create_task(self._run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _once(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "nycti.db.sqlite_archive", "upload", "--source", self.source,
            env={**os.environ, **self.config.environment()},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode:
                raise RuntimeError("Backup child failed")
            result = json.loads(output)
            if not result.get("verified") and not result.get("skipped"):
                raise ValueError("Invalid backup result")
            LOGGER.info("sqlite_backup %s", json.dumps(result, sort_keys=True))
            return max(60, min(86400, int(result["next_seconds"])))
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def _run(self):
        while True:
            try:
                delay = await self._once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error("sqlite_backup_failed error_type=%s retry_seconds=3600", type(error).__name__)
                delay = 3600
            await asyncio.sleep(delay)
