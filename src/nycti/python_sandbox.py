from __future__ import annotations

import ast
from contextlib import redirect_stdout
from dataclasses import dataclass
import io
import math
import signal
import statistics
import time
from typing import Any


class PythonSandboxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PythonSandboxResult:
    output: str
    elapsed_ms: int
    truncated: bool = False


SAFE_BUILTINS: dict[str, object] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "pow": pow,
    "print": print,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
SAFE_GLOBALS: dict[str, object] = {
    "__builtins__": SAFE_BUILTINS,
    "math": math,
    "statistics": statistics,
}
FORBIDDEN_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
    "vars",
}
FORBIDDEN_NODE_TYPES = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.FunctionDef,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


def run_python_sandbox(
    code: str,
    *,
    timeout_seconds: float,
    max_output_chars: int,
) -> PythonSandboxResult:
    started_at = time.perf_counter()
    tree = ast.parse(code, mode="exec")
    _validate_tree(tree)
    stdout = io.StringIO()
    locals_scope: dict[str, Any] = {}
    try:
        with _execution_timer(timeout_seconds), redirect_stdout(stdout):
            exec(compile(tree, "<nycti-python-tool>", "exec"), dict(SAFE_GLOBALS), locals_scope)
    except TimeoutError as exc:
        raise PythonSandboxError("Python execution exceeded the configured timeout.") from exc
    elapsed_ms = round(max(time.perf_counter() - started_at, 0.0) * 1000)
    if elapsed_ms > timeout_seconds * 1000:
        raise PythonSandboxError("Python execution exceeded the configured timeout.")
    output_parts: list[str] = []
    printed = stdout.getvalue().strip()
    if printed:
        output_parts.append(printed)
    if "result" in locals_scope:
        output_parts.append(f"result = {locals_scope['result']!r}")
    output = "\n".join(output_parts).strip() or "(no output; assign `result` or print something)"
    truncated = len(output) > max_output_chars
    if truncated:
        output = output[: max_output_chars - 14].rstrip() + "\n[truncated]"
    return PythonSandboxResult(output=output, elapsed_ms=elapsed_ms, truncated=truncated)


def _validate_tree(tree: ast.AST) -> None:
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > 1500:
            raise PythonSandboxError("Python code is too large.")
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise PythonSandboxError(f"Python node `{type(node).__name__}` is not allowed.")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise PythonSandboxError(f"Python name `{node.id}` is not allowed.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise PythonSandboxError("Private or dunder attribute access is not allowed.")
        if isinstance(node, ast.Call):
            _validate_call(node)


def _validate_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in SAFE_BUILTINS and name not in {"math", "statistics"}:
            raise PythonSandboxError(f"Python call `{name}` is not allowed.")
        return
    if isinstance(node.func, ast.Attribute):
        if not isinstance(node.func.value, ast.Name) or node.func.value.id not in {"math", "statistics"}:
            raise PythonSandboxError("Only math.* and statistics.* function calls are allowed.")
        if node.func.attr.startswith("_"):
            raise PythonSandboxError("Private or dunder attribute access is not allowed.")
        return
    raise PythonSandboxError("Only direct safe function calls are allowed.")


class _execution_timer:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = max(timeout_seconds, 0.1)
        self._previous_handler = None
        self._previous_timer: tuple[float, float] | None = None

    def __enter__(self) -> None:
        if not hasattr(signal, "setitimer"):
            return
        self._previous_handler = signal.getsignal(signal.SIGALRM)
        self._previous_timer = signal.setitimer(signal.ITIMER_REAL, self.timeout_seconds)
        signal.signal(signal.SIGALRM, self._handle_timeout)

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        if not hasattr(signal, "setitimer"):
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self._previous_handler is not None:
            signal.signal(signal.SIGALRM, self._previous_handler)
        if self._previous_timer is not None and self._previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *self._previous_timer)

    @staticmethod
    def _handle_timeout(signum, frame) -> None:  # type: ignore[no-untyped-def]
        raise TimeoutError("Python sandbox timed out.")
