from __future__ import annotations

import ast
from contextlib import redirect_stdout
import json
import math
import reprlib
import resource
import statistics
import sys
import time
from typing import Any


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


class SandboxValidationError(ValueError):
    pass


class BoundedWriter:
    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 100)
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def write(self, value: str) -> int:
        text = str(value)
        remaining = self.limit - self.size
        if remaining > 0:
            self.parts.append(text[:remaining])
            self.size += min(len(text), remaining)
        if len(text) > remaining:
            self.truncated = True
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return "".join(self.parts)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        code = str(payload["code"])
        timeout_seconds = max(float(payload["timeout_seconds"]), 0.1)
        max_output_chars = max(int(payload["max_output_chars"]), 100)
        memory_limit_mb = max(int(payload.get("memory_limit_mb", 128)), 64)
        _set_resource_limits(timeout_seconds=timeout_seconds, memory_limit_mb=memory_limit_mb)
        result = _execute(code, max_output_chars=max_output_chars)
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        response = {"ok": True, **result}
    sys.stdout.write(json.dumps(response, ensure_ascii=True))


def _execute(code: str, *, max_output_chars: int) -> dict[str, object]:
    started_at = time.perf_counter()
    tree = ast.parse(code, mode="exec")
    _validate_tree(tree)
    stdout = BoundedWriter(max_output_chars)
    locals_scope: dict[str, Any] = {}
    with redirect_stdout(stdout):
        exec(compile(tree, "<nycti-python-tool>", "exec"), dict(SAFE_GLOBALS), locals_scope)
    output_parts: list[str] = []
    printed = stdout.getvalue().strip()
    if printed:
        output_parts.append(printed)
    if "result" in locals_scope:
        renderer = reprlib.Repr()
        renderer.maxstring = max_output_chars
        renderer.maxother = max_output_chars
        output_parts.append(f"result = {renderer.repr(locals_scope['result'])}")
    output = "\n".join(output_parts).strip() or "(no output; assign `result` or print something)"
    truncated = stdout.truncated or len(output) > max_output_chars
    if len(output) > max_output_chars:
        output = output[: max_output_chars - 14].rstrip() + "\n[truncated]"
    return {
        "output": output,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "truncated": truncated,
    }


def _set_resource_limits(*, timeout_seconds: float, memory_limit_mb: int) -> None:
    memory_bytes = memory_limit_mb * 1024 * 1024
    for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
        limit = getattr(resource, limit_name, None)
        if limit is not None:
            try:
                resource.setrlimit(limit, (memory_bytes, memory_bytes))
            except (OSError, ValueError):
                pass
    cpu_seconds = max(int(math.ceil(timeout_seconds)), 1)
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (OSError, ValueError):
        pass


def _validate_tree(tree: ast.AST) -> None:
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > 1500:
            raise SandboxValidationError("Python code is too large.")
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise SandboxValidationError(f"Python node `{type(node).__name__}` is not allowed.")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise SandboxValidationError(f"Python name `{node.id}` is not allowed.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise SandboxValidationError("Private or dunder attribute access is not allowed.")
        if isinstance(node, ast.Call):
            _validate_call(node)


def _validate_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in SAFE_BUILTINS:
            raise SandboxValidationError(f"Python call `{name}` is not allowed.")
        return
    if isinstance(node.func, ast.Attribute):
        if not isinstance(node.func.value, ast.Name) or node.func.value.id not in {"math", "statistics"}:
            raise SandboxValidationError("Only math.* and statistics.* function calls are allowed.")
        if node.func.attr.startswith("_"):
            raise SandboxValidationError("Private or dunder attribute access is not allowed.")
        return
    raise SandboxValidationError("Only direct safe function calls are allowed.")


if __name__ == "__main__":
    main()
