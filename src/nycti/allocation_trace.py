"""Temporary Python allocation attribution; no object values or heap dumps."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys
import time

LOGGER = logging.getLogger(__name__)
TRACE_DURATION_SECONDS = 600
TRACE_REPORT_COOLDOWN_SECONDS = 15
TRACE_TOP_LIMIT = 15


class AllocationTracer:
    def __init__(self) -> None:
        self.baseline = None
        self.started_at: float | None = None
        self.last_report: float | None = None
        self.timer = None
        self.owns_tracing = False
        self.has_traced = False

    def status(self) -> dict[str, object]:
        tracer = sys.modules.get("tracemalloc")
        active = tracer is not None and tracer.is_tracing()
        self.has_traced = self.has_traced or active
        result = {"active": active, "owned": self.owns_tracing, "has_traced": self.has_traced}
        if active:
            current, peak = tracer.get_traced_memory()
            result.update(traced_current_bytes=current, traced_peak_bytes=peak,
                          tracer_metadata_bytes=tracer.get_tracemalloc_memory())
        if self.started_at is not None:
            result["elapsed_seconds"] = round(time.monotonic() - self.started_at, 3)
            result["duration_limit_seconds"] = TRACE_DURATION_SECONDS
        return result

    def _filtered(self, snapshot):
        import tracemalloc

        directory = Path(__file__).parent
        excluded = [tracemalloc.__file__, *(str(directory / name) for name in (
            "allocation_trace.py", "resource_profile.py", "resource_metrics.py",
        ))]
        return snapshot.filter_traces([tracemalloc.Filter(False, name) for name in excluded])

    def start(self) -> None:
        import tracemalloc

        if self.owns_tracing or tracemalloc.is_tracing():
            LOGGER.warning("allocation_trace start ignored: tracing is already active; deadline unchanged")
            return
        try:
            loop = asyncio.get_running_loop()
            tracemalloc.start(1)
            self.owns_tracing = True
            self.has_traced = True
            self.started_at = time.monotonic()
            self.last_report = None
            self.baseline = self._filtered(tracemalloc.take_snapshot())
            self.timer = loop.call_later(TRACE_DURATION_SECONDS, self.stop, "timeout")
            LOGGER.info("allocation_trace %s", json.dumps({
                "phase": "started", "frames": 1, "duration_limit_seconds": TRACE_DURATION_SECONDS,
                "existing_allocations_included": False, **self.status(),
            }, separators=(",", ":")))
        except Exception:
            self.close()
            LOGGER.warning("allocation_trace start failed")

    def report(self, *, phase: str = "snapshot", force: bool = False) -> None:
        if not self.owns_tracing or self.baseline is None:
            return
        now = time.monotonic()
        if not force and self.last_report is not None and now - self.last_report < TRACE_REPORT_COOLDOWN_SECONDS:
            return
        self.last_report = now
        try:
            import tracemalloc

            started = time.perf_counter()
            snapshot = self._filtered(tracemalloc.take_snapshot())
            changes = snapshot.compare_to(self.baseline, "lineno")
            growing = sorted((item for item in changes if item.size_diff > 0),
                             key=lambda item: item.size_diff, reverse=True)
            payload = {
                "phase": phase, **self.status(),
                "net_change_bytes": sum(item.size_diff for item in changes),
                "growing_locations": len(growing),
                "top_growth": [{
                    "file": _display_path(item.traceback[0].filename), "line": item.traceback[0].lineno,
                    "retained_bytes": item.size, "change_bytes": item.size_diff,
                    "blocks": item.count, "change_blocks": item.count_diff,
                } for item in growing[:TRACE_TOP_LIMIT]],
                "capture_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            LOGGER.info("allocation_trace %s", json.dumps(payload, separators=(",", ":")))
        except Exception:
            LOGGER.warning("allocation_trace snapshot failed")

    def stop(self, reason: str = "manual") -> None:
        if not self.owns_tracing:
            return
        try:
            self.report(phase=f"stopped_{reason}", force=True)
        finally:
            self.close()

    def close(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.baseline = None
        self.started_at = None
        if self.owns_tracing:
            import tracemalloc

            self.owns_tracing = False
            tracemalloc.stop()


def _display_path(filename: str) -> str:
    # Keep module attribution without exposing machine-specific home-directory names.
    path = Path(filename)
    for marker in ("site-packages", "nycti"):
        if marker in path.parts:
            return "/".join(path.parts[path.parts.index(marker):])[-240:]
    try:
        return str(path.relative_to(sys.base_prefix))[-240:]
    except ValueError:
        return path.name[-240:]
