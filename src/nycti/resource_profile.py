"""On-demand, content-free resource snapshots for an authorized container shell."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import gc
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

LOGGER = logging.getLogger(__name__)
PROFILE_COOLDOWN_SECONDS = 15


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _status(path: Path) -> dict[str, str]:
    result = {}
    for line in _read(path).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            result[key] = value.strip()
    return result


def _text_storage(strings) -> int:
    # Shallow string storage, not full heap size; shared strings count once per collection.
    seen: set[int] = set()
    total = 0
    for value in strings:
        if isinstance(value, str) and id(value) not in seen:
            seen.add(id(value))
            total += sys.getsizeof(value)
    return total


def collect_resource_profile(bot: Any) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    status = _status(Path("/proc/self/status"))
    process = {}
    for key in ("VmRSS", "VmHWM", "RssAnon", "RssFile", "VmSwap"):
        if key in status:
            process[key + "_bytes"] = int(status[key].split()[0]) * 1024
    if "Threads" in status:
        process["threads"] = int(status["Threads"])
    container = {}
    raw = _read(Path("/sys/fs/cgroup/memory.current")).strip()
    if raw.isdigit():
        container["memory_current_bytes"] = int(raw)
    for line in _read(Path("/sys/fs/cgroup/memory.stat")).splitlines():
        key, value = line.split()
        if key in {"anon", "file", "kernel", "shmem"}:
            container[key + "_bytes"] = int(value)

    messages = bot.cached_messages
    cache = bot._response_diagnostic_cache
    snapshots = cache._snapshots
    snapshot_strings = (
        value for snapshot in snapshots for value in
        (snapshot.prompt, snapshot.reply_text, *snapshot.context_lines,
         *snapshot.image_context_lines, *snapshot.metrics.values())
    )
    queues = {}
    workers = {
        "memory": bot._background_memory_writer._jobs,
        "emoji": bot._emoji_learner.jobs,
        "telemetry": bot._chat_orchestrator.telemetry_writer._jobs,
    }
    if bot._background_procedure_learner is not None:
        workers["procedures"] = bot._background_procedure_learner._jobs
    for name, worker in workers.items():
        queues[name] = {
            "pending": worker.pending_count, "capacity": worker.queue.maxsize,
            "running": worker.task is not None and not worker.task.done(),
        }
    pool = bot.database.engine.pool
    pool_counts = {key: method() for key in ("size", "checkedin", "checkedout", "overflow")
                   if callable(method := getattr(pool, key, None))}
    return {
        "schema_version": 1, "timestamp_utc": now.isoformat(), "pid": os.getpid(),
        "uptime_seconds": round((now - bot.started_at_utc).total_seconds()),
        "process": process, "container": container,
        "loaded_modules": {name: name in sys.modules for name in ("numpy", "networkx", "playwright.async_api")},
        "gc_counts": gc.get_count(),
        "active_requests": sum(not task.done() for task in bot._active_requests._tasks.values()),
        "discord_cache": {
            "messages": len(messages), "capacity": bot._connection.max_messages,
            "text_shallow_bytes": _text_storage(message.content for message in messages),
        },
        "response_diagnostics": {
            "entries": len(snapshots), "capacity": cache.max_entries,
            "expired_entries": sum(now - item.captured_at > cache.max_age for item in snapshots),
            "text_shallow_bytes": _text_storage(snapshot_strings),
        },
        "emoji_evidence": {
            "entries": len(bot._emoji_learner.evidence),
            "samples": sum(len(item.samples) for item in bot._emoji_learner.evidence.values()),
        },
        "queues": queues, "database_pool": pool_counts,
    }


class ResourceProfiler:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.last_emitted: float | None = None

    def emit(self) -> None:
        now = time.monotonic()
        if self.last_emitted is not None and now - self.last_emitted < PROFILE_COOLDOWN_SECONDS:
            return
        self.last_emitted = now
        try:
            LOGGER.info("resource_profile %s", json.dumps(collect_resource_profile(self.bot), separators=(",", ":")))
        except Exception:
            # Never log object reprs or exception details that could contain private state.
            LOGGER.warning("resource_profile collection failed")


@contextmanager
def resource_profile_hook(bot: Any):
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        yield
        return
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(sig)
    try:
        loop.add_signal_handler(sig, ResourceProfiler(bot).emit)
    except (NotImplementedError, RuntimeError, ValueError):
        LOGGER.warning("On-demand resource profiling signals are unavailable on this platform.")
        yield
        return
    try:
        yield
    finally:
        loop.remove_signal_handler(sig)
        signal.signal(sig, previous)


def request_resource_profile(pid: int) -> None:
    """Refuse to signal an unrelated process or one without an installed handler."""
    sig = getattr(signal, "SIGUSR1", None)
    if pid <= 0 or sig is None:
        raise ValueError("A positive PID and Unix signal support are required.")
    command = _read(Path(f"/proc/{pid}/cmdline")).split("\0")
    caught = _status(Path(f"/proc/{pid}/status")).get("SigCgt", "0")
    if "nycti.main" not in command or not int(caught, 16) & (1 << (sig - 1)):
        raise ValueError("Target is not a Nycti runtime with a registered profiling handler.")
    os.kill(pid, sig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Request a numeric-only live Nycti resource snapshot in its logs.")
    parser.add_argument("--pid", type=int, default=1)
    args = parser.parse_args()
    try:
        request_resource_profile(args.pid)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Could not request resource profile: {exc}\n")
    print(f"Resource profile requested for PID {args.pid}; inspect logs for resource_profile (15s cooldown).")


if __name__ == "__main__":
    main()
