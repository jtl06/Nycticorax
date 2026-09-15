"""Small runtime/allocator counters; never heap contents or forced collection."""
from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
import sys
import time


@lru_cache(maxsize=1)
def _allocator_reader():
    if sys.platform != "linux":
        return None
    import ctypes

    class Mallinfo2(ctypes.Structure):
        _fields_ = [(name, ctypes.c_size_t) for name in (
            "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
            "fsmblks", "uordblks", "fordblks", "keepcost",
        )]

    try:
        function = ctypes.CDLL(None).mallinfo2
    except (OSError, AttributeError):
        return None
    function.argtypes = []
    function.restype = Mallinfo2
    return function


def allocator_counters() -> dict[str, int]:
    reader = _allocator_reader()
    if reader is None:
        return {}
    # Called on the event loop, not inside a raw OS signal handler. Never calls mallopt/trim.
    info = reader()
    return {"arena_bytes": info.arena, "in_use_bytes": info.uordblks,
            "free_bytes": info.fordblks, "mmap_bytes": info.hblkhd}


def runtime_counters() -> dict[str, int | float]:
    result = {"cpu_seconds": round(time.process_time(), 6)}
    try:
        result["file_descriptors"] = len(list(Path("/proc/self/fd").iterdir()))
    except OSError:
        pass
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return result
    result["asyncio_tasks"] = len(asyncio.all_tasks(loop))
    executor = getattr(loop, "_default_executor", None)
    if executor is None:
        result.update(executor_threads=0, executor_pending=0)
    else:
        threads = getattr(executor, "_threads", None)
        if threads is not None:
            result["executor_threads"] = sum(thread.is_alive() for thread in threads)
        queue = getattr(executor, "_work_queue", None)
        if queue is not None and hasattr(queue, "qsize"):
            result["executor_pending"] = queue.qsize()
    return result
