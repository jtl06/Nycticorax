from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor


def configure_io_workers(max_workers: int) -> None:
    """Opt into a smaller blocking-I/O pool before clients submit work.

    asyncio.run owns shutdown. Zero preserves Python's platform default;
    this is a startup setting, not an idle-time executor replacement.
    """
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 0 <= max_workers <= 32:
        raise ValueError("IO_MAX_WORKERS must be between 0 and 32")
    if max_workers:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nycti-io")
        )
