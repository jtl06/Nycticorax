from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time

from nycti.timing import elapsed_ms


class PythonSandboxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PythonSandboxResult:
    output: str
    elapsed_ms: int
    truncated: bool = False


def run_python_sandbox(
    code: str,
    *,
    timeout_seconds: float,
    max_output_chars: int,
) -> PythonSandboxResult:
    started_at = time.perf_counter()
    worker_path = Path(__file__).with_name("python_sandbox_worker.py")
    payload = json.dumps(
        {
            "code": code,
            "timeout_seconds": max(timeout_seconds, 0.1),
            "max_output_chars": max(max_output_chars, 100),
            "memory_limit_mb": 128,
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(worker_path)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 0.1) + 0.75,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PythonSandboxError("Python execution exceeded the configured timeout.") from exc

    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[:240]
        suffix = f": {detail}" if detail else ""
        raise PythonSandboxError(f"Python worker exited unexpectedly{suffix}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PythonSandboxError("Python worker returned an invalid response.") from exc
    if not isinstance(result, dict):
        raise PythonSandboxError("Python worker returned an invalid response.")
    if not bool(result.get("ok")):
        raise PythonSandboxError(str(result.get("error") or "Python execution failed."))
    return PythonSandboxResult(
        output=str(result.get("output") or ""),
        elapsed_ms=int(result.get("elapsed_ms") or elapsed_ms(started_at)),
        truncated=bool(result.get("truncated")),
    )
