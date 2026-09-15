"""On-demand, content-free resource snapshots for an authorized container shell."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
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

from nycti.resource_metrics import allocator_counters, runtime_counters
from nycti.allocation_trace import AllocationTracer

LOGGER = logging.getLogger(__name__)
PROFILE_COOLDOWN_SECONDS = 15
PROFILE_HISTORY_LIMIT = 120


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
            "active": getattr(worker, "active", False),
            "running": worker.task is not None and not worker.task.done(),
        }
    pool = bot.database.engine.pool
    pool_counts = {key: method() for key in ("size", "checkedin", "checkedout", "overflow")
                   if callable(method := getattr(pool, key, None))}
    return {
        "schema_version": 2, "timestamp_utc": now.isoformat(), "pid": os.getpid(),
        "uptime_seconds": round((now - bot.started_at_utc).total_seconds()),
        "process": process, "container": container,
        "runtime": runtime_counters(), "native_allocator": allocator_counters(),
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
        self.last_sampled: float | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=PROFILE_HISTORY_LIMIT)
        self.allocations = AllocationTracer()

    def _capture(self, now: float) -> dict[str, object]:
        self.last_sampled = now
        started = time.perf_counter()
        profile = collect_resource_profile(self.bot)
        profile["allocation_tracing"] = self.allocations.status()
        queues = profile.get("queues", {})
        sample = {
            "monotonic_seconds": now, "timestamp_utc": profile.get("timestamp_utc"),
            "tracing": profile["allocation_tracing"]["active"],
            "tracing_seen": profile["allocation_tracing"].get("has_traced", profile["allocation_tracing"]["active"]),
            "idle": profile.get("active_requests") == 0 and bool(queues) and all(
                not item.get("active", True) and item.get("pending") == 0 for item in queues.values()
            ),
        }
        for source, key, output in (
            ("process", "VmRSS_bytes", "rss_bytes"),
            ("process", "RssAnon_bytes", "anon_bytes"),
            ("process", "threads", "threads"),
            ("container", "memory_current_bytes", "container_bytes"),
            ("runtime", "cpu_seconds", "cpu_seconds"),
            ("runtime", "executor_threads", "executor_threads"),
            ("runtime", "file_descriptors", "file_descriptors"),
            ("native_allocator", "free_bytes", "allocator_free_bytes"),
        ):
            value = profile.get(source, {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sample[output] = value
        sample["capture_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.history.append(sample)
        return profile

    def summary(self) -> dict[str, object]:
        rows = list(self.history)
        summary = {"sample_count": len(rows), "capacity": PROFILE_HISTORY_LIMIT, "recent": rows[-6:]}
        if rows:
            summary["window_seconds"] = round(rows[-1]["monotonic_seconds"] - rows[0]["monotonic_seconds"], 3)
        for label, selected in (("rss", [row for row in rows if "rss_bytes" in row]),
                                ("idle_rss", [row for row in rows if row["idle"] and not row["tracing_seen"] and "rss_bytes" in row]),
                                ("traced_rss", [row for row in rows if row["tracing"] and "rss_bytes" in row]),
                                ("post_trace_rss", [row for row in rows if row["tracing_seen"] and not row["tracing"] and "rss_bytes" in row])):
            if not selected:
                continue
            values = [row["rss_bytes"] for row in selected]
            summary[label] = {"sample_count": len(values), "min_bytes": min(values), "peak_bytes": max(values),
                              "change_bytes": values[-1] - values[0],
                              "window_seconds": round(selected[-1]["monotonic_seconds"] - selected[0]["monotonic_seconds"], 3)}
        return summary

    def sample_if_due(self) -> None:
        interval = getattr(getattr(self.bot, "settings", None), "resource_profile_interval_seconds", 300)
        if not interval:
            return
        now = time.monotonic()
        if self.last_sampled is not None and now - self.last_sampled < interval:
            return
        try:
            self._capture(now)
            LOGGER.info("resource_sample %s", json.dumps(self.history[-1], separators=(",", ":")))
        except Exception:
            LOGGER.warning("resource_profile collection failed")

    def emit(self) -> None:
        now = time.monotonic()
        if self.last_emitted is not None and now - self.last_emitted < PROFILE_COOLDOWN_SECONDS:
            return
        self.last_emitted = now
        try:
            profile = self._capture(now)
            profile["history"] = self.summary()
            LOGGER.info("resource_profile %s", json.dumps(profile, separators=(",", ":")))
            self.allocations.report()
        except Exception:
            # Never log object reprs or exception details that could contain private state.
            LOGGER.warning("resource_profile collection failed")


@contextmanager
def resource_profile_hook(bot: Any):
    profiler = ResourceProfiler(bot)
    bot._resource_profiler = profiler
    try:
        with _signal_hook(profiler):
            yield
    finally:
        profiler.allocations.close()
        del bot._resource_profiler


@contextmanager
def _signal_hook(profiler: ResourceProfiler):
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        yield
        return
    loop = asyncio.get_running_loop()
    installed = {}
    callbacks = [(sig, profiler.emit), (getattr(signal, "SIGUSR2", None), profiler.allocations.start),
                 (getattr(signal, "SIGRTMIN", None), profiler.allocations.stop)]
    try:
        for number, callback in callbacks:
            if number is not None:
                previous = signal.getsignal(number)
                loop.add_signal_handler(number, callback)
                installed[number] = previous
    except (NotImplementedError, RuntimeError, ValueError):
        for number, previous in installed.items():
            loop.remove_signal_handler(number)
            signal.signal(number, previous)
        LOGGER.warning("On-demand resource profiling signals are unavailable on this platform.")
        yield
        return
    try:
        yield
    finally:
        for number, previous in installed.items():
            loop.remove_signal_handler(number)
            signal.signal(number, previous)


def request_resource_profile(pid: int, *, trace_action: str | None = None) -> None:
    """Refuse to signal an unrelated process or one without an installed handler."""
    names = {None: "SIGUSR1", "snapshot": "SIGUSR1", "start": "SIGUSR2", "stop": "SIGRTMIN"}
    if trace_action not in names:
        raise ValueError("Unknown allocation-trace action.")
    sig = getattr(signal, names[trace_action], None)
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
    parser.add_argument("--trace", choices=("start", "snapshot", "stop"),
                        help="Temporary allocation tracing: automatic stop after ten minutes; adds RAM/CPU overhead.")
    args = parser.parse_args()
    try:
        request_resource_profile(args.pid, trace_action=args.trace)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Could not request resource profile: {exc}\n")
    if args.trace:
        print(f"Allocation trace {args.trace} requested for PID {args.pid}; inspect allocation_trace logs.")
    else:
        print(f"Resource profile requested for PID {args.pid}; inspect logs for resource_profile (15s cooldown).")


if __name__ == "__main__":
    main()
