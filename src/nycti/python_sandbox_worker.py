from __future__ import annotations

import ast
from contextlib import redirect_stdout
import importlib
import json
import math
import reprlib
import resource
import statistics
import sys
import time
from typing import Any


SAFE_MODULE_EXPORTS: dict[str, frozenset[str]] = {
    "math": frozenset(name for name in dir(math) if not name.startswith("_")),
    "statistics": frozenset(name for name in dir(statistics) if not name.startswith("_")),
    "numpy": frozenset(
        {
            "abs",
            "all",
            "any",
            "arange",
            "argmax",
            "argmin",
            "array",
            "average",
            "clip",
            "concatenate",
            "corrcoef",
            "cov",
            "cumprod",
            "cumsum",
            "diff",
            "dot",
            "e",
            "exp",
            "eye",
            "full",
            "geomspace",
            "linspace",
            "log",
            "log10",
            "logspace",
            "matmul",
            "max",
            "mean",
            "median",
            "min",
            "ones",
            "percentile",
            "pi",
            "polyfit",
            "polyval",
            "prod",
            "quantile",
            "reshape",
            "round",
            "sort",
            "sqrt",
            "stack",
            "std",
            "sum",
            "transpose",
            "unique",
            "var",
            "where",
            "zeros",
        }
    ),
    "networkx": frozenset(
        {
            "Graph",
            "DiGraph",
            "MultiGraph",
            "MultiDiGraph",
            "all_pairs_shortest_path_length",
            "average_clustering",
            "barabasi_albert_graph",
            "betweenness_centrality",
            "bfs_edges",
            "bfs_tree",
            "center",
            "closeness_centrality",
            "clustering",
            "complete_graph",
            "compose",
            "connected_components",
            "cycle_graph",
            "degree_centrality",
            "density",
            "dfs_edges",
            "dfs_tree",
            "diameter",
            "disjoint_union",
            "eigenvector_centrality",
            "erdos_renyi_graph",
            "find_cycle",
            "from_edgelist",
            "has_path",
            "is_connected",
            "is_directed_acyclic_graph",
            "is_tree",
            "minimum_spanning_tree",
            "node_connected_component",
            "number_connected_components",
            "pagerank",
            "path_graph",
            "radius",
            "relabel_nodes",
            "shortest_path",
            "shortest_path_length",
            "star_graph",
            "topological_sort",
            "transitivity",
            "watts_strogatz_graph",
            "weakly_connected_components",
        }
    ),
}
SAFE_OBJECT_METHODS = frozenset(
    {
        "add_edge",
        "add_edges_from",
        "add_node",
        "add_nodes_from",
        "append",
        "astype",
        "clear",
        "copy",
        "count",
        "degree",
        "edges",
        "extend",
        "flatten",
        "get",
        "has_edge",
        "has_node",
        "in_degree",
        "index",
        "is_directed",
        "items",
        "keys",
        "max",
        "mean",
        "min",
        "neighbors",
        "nodes",
        "number_of_edges",
        "number_of_nodes",
        "out_degree",
        "ravel",
        "remove_edge",
        "remove_edges_from",
        "remove_node",
        "remove_nodes_from",
        "reshape",
        "round",
        "sort",
        "std",
        "subgraph",
        "sum",
        "to_directed",
        "to_undirected",
        "tolist",
        "transpose",
        "update",
        "values",
        "var",
    }
)


class SafeModuleProxy:
    def __init__(self, module_name: str, module: object) -> None:
        self._module_name = module_name
        self._module = module

    def __getattr__(self, name: str) -> object:
        if name not in SAFE_MODULE_EXPORTS[self._module_name]:
            raise AttributeError(f"{self._module_name}.{name} is not allowed")
        return getattr(self._module, name)


_SAFE_MODULE_CACHE: dict[str, SafeModuleProxy] = {}


def _safe_import(
    name: str,
    globals: object = None,
    locals: object = None,
    fromlist: object = (),
    level: int = 0,
) -> SafeModuleProxy:
    del globals, locals, fromlist
    if level != 0 or name not in SAFE_MODULE_EXPORTS:
        raise ImportError(
            f"Module `{name}` is not allowed; use math, statistics, numpy, or networkx."
        )
    proxy = _SAFE_MODULE_CACHE.get(name)
    if proxy is None:
        module = importlib.import_module(name)
        proxy = SafeModuleProxy(name, module)
        _SAFE_MODULE_CACHE[name] = proxy
    return proxy


SAFE_BUILTINS: dict[str, object] = {
    "__import__": _safe_import,
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
    "math": SafeModuleProxy("math", math),
    "statistics": SafeModuleProxy("statistics", statistics),
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
    module_aliases, imported_names = _collect_import_bindings(tree)
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > 1500:
            raise SandboxValidationError("Python code is too large.")
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise SandboxValidationError(f"Python node `{type(node).__name__}` is not allowed.")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise SandboxValidationError(f"Python name `{node.id}` is not allowed.")
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in module_aliases.keys() | imported_names
        ):
            raise SandboxValidationError(f"Imported name `{node.id}` cannot be reassigned.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise SandboxValidationError("Private or dunder attribute access is not allowed.")
        if isinstance(node, ast.Call):
            _validate_call(
                node,
                module_aliases=module_aliases,
                imported_names=imported_names,
            )


def _collect_import_bindings(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    module_aliases = {"math": "math", "statistics": "statistics"}
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in SAFE_MODULE_EXPORTS:
                    raise SandboxValidationError(
                        f"Module `{alias.name}` is not allowed; use math, statistics, numpy, or networkx."
                    )
                local_name = alias.asname or alias.name
                _validate_imported_name(local_name)
                module_aliases[local_name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module_name = str(node.module or "")
            if node.level != 0 or module_name not in SAFE_MODULE_EXPORTS:
                raise SandboxValidationError(
                    f"Module `{module_name}` is not allowed; use math, statistics, numpy, or networkx."
                )
            for alias in node.names:
                if alias.name == "*" or alias.name not in SAFE_MODULE_EXPORTS[module_name]:
                    raise SandboxValidationError(
                        f"Import `{module_name}.{alias.name}` is not allowed."
                    )
                local_name = alias.asname or alias.name
                _validate_imported_name(local_name)
                imported_names.add(local_name)
    return module_aliases, imported_names


def _validate_imported_name(name: str) -> None:
    if not name.isidentifier() or name.startswith("_") or name in FORBIDDEN_NAMES:
        raise SandboxValidationError(f"Imported name `{name}` is not allowed.")


def _validate_call(
    node: ast.Call,
    *,
    module_aliases: dict[str, str],
    imported_names: set[str],
) -> None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in SAFE_BUILTINS and name not in imported_names:
            raise SandboxValidationError(f"Python call `{name}` is not allowed.")
        return
    if isinstance(node.func, ast.Attribute):
        if node.func.attr.startswith("_"):
            raise SandboxValidationError("Private or dunder attribute access is not allowed.")
        if isinstance(node.func.value, ast.Name) and node.func.value.id in module_aliases:
            module_name = module_aliases[node.func.value.id]
            if node.func.attr not in SAFE_MODULE_EXPORTS[module_name]:
                raise SandboxValidationError(
                    f"Python call `{module_name}.{node.func.attr}` is not allowed."
                )
            return
        if node.func.attr in SAFE_OBJECT_METHODS:
            return
        raise SandboxValidationError(f"Python method `{node.func.attr}` is not allowed.")
    raise SandboxValidationError("Only direct safe function calls are allowed.")


if __name__ == "__main__":
    main()
