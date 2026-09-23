"""Provider-free paired thread-pool experiment; not a Discord or model benchmark."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from nycti.io_workers import configure_io_workers


def rss_kib() -> int:
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True))


async def sample(workers: int, tasks: int) -> dict[str, object]:
    configure_io_workers(workers)
    payload = json.dumps({"prices": list(range(50_000))})
    idle_before = rss_kib()
    lock = threading.Lock()
    active = peak = 0

    def fake_io() -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            data = json.loads(payload)
            time.sleep(0.05)
            return len(data["prices"])
        finally:
            with lock:
                active -= 1

    started = time.perf_counter()
    results = await asyncio.gather(*(asyncio.to_thread(fake_io) for _ in range(tasks)))
    wall_ms = round((time.perf_counter() - started) * 1000, 2)
    await asyncio.sleep(0.1)
    executor = asyncio.get_running_loop()._default_executor
    return {
        "workers": workers, "tasks": tasks, "wall_ms": wall_ms,
        "idle_before_kib": idle_before, "idle_after_kib": rss_kib(),
        "threads": len(executor._threads), "peak_active": peak,
        "results_ok": results == [50_000] * tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, choices=(24, 32))
    parser.add_argument("--tasks", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sample:
        print(json.dumps(asyncio.run(sample(args.sample, args.tasks))))
        return
    rows = []
    for tasks in (24, 48):
        for pair in range(3):
            for workers in ((32, 24) if pair % 2 == 0 else (24, 32)):
                raw = subprocess.check_output(
                    [sys.executable, __file__, "--sample", str(workers), "--tasks", str(tasks)],
                    text=True,
                )
                rows.append({"pair": pair + 1, **json.loads(raw)})
    report = {
        "kind": "synthetic_thread_pool_only", "platform": sys.platform,
        "python": sys.version, "baseline": "0450e3f",
        "workload": "24 concurrent fake I/O reads (10 two-provider quotes + 4 searches); 48-read overlap stress",
        "limitations": "50ms sleeps plus JSON parsing; no provider/Discord calls. Not production RSS or e2e evidence.",
        "samples": rows,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
