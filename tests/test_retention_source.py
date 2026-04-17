import ast
from pathlib import Path
import unittest


def _get_class_method(tree: ast.Module, *, class_name: str, method_name: str) -> ast.AsyncFunctionDef:
    klass = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if klass is None:
        raise AssertionError(f"Class {class_name} not found.")
    method = next(
        (
            node
            for node in klass.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name
        ),
        None,
    )
    if method is None:
        raise AssertionError(f"Method {class_name}.{method_name} not found.")
    return method


def _collect_call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        if isinstance(item.func, ast.Name):
            names.add(item.func.id)
        elif isinstance(item.func, ast.Attribute):
            names.add(item.func.attr)
    return names


class RetentionSourceTests(unittest.TestCase):
    def test_reminder_service_prunes_delivered_rows(self) -> None:
        tree = ast.parse(Path("src/nycti/reminders/service.py").read_text())
        method = _get_class_method(
            tree,
            class_name="ReminderService",
            method_name="prune_delivered_before",
        )
        call_names = _collect_call_names(method)
        self.assertIn("delete", call_names)

        touched_attrs = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
        }
        self.assertIn("delivered_at", touched_attrs)

    def test_memory_service_prunes_stale_memories(self) -> None:
        tree = ast.parse(Path("src/nycti/memory/service.py").read_text())
        method = _get_class_method(
            tree,
            class_name="MemoryService",
            method_name="prune_stale_memories",
        )
        call_names = _collect_call_names(method)
        self.assertIn("delete", call_names)
        self.assertIn("or_", call_names)
        self.assertIn("and_", call_names)

        touched_attrs = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
        }
        self.assertIn("times_retrieved", touched_attrs)
        self.assertIn("last_retrieved_at", touched_attrs)
        self.assertIn("created_at", touched_attrs)

    def test_bot_runs_all_retention_paths(self) -> None:
        tree = ast.parse(Path("src/nycti/bot.py").read_text())
        maintenance = _get_class_method(
            tree,
            class_name="NyctiBot",
            method_name="_run_retention_maintenance",
        )
        maintenance_calls = _collect_call_names(maintenance)
        self.assertIn("prune_usage_events_before", maintenance_calls)
        self.assertIn("prune_delivered_before", maintenance_calls)
        self.assertIn("prune_stale_memories", maintenance_calls)

        setup_hook = _get_class_method(tree, class_name="NyctiBot", method_name="setup_hook")
        setup_calls = _collect_call_names(setup_hook)
        self.assertIn("_run_retention_maintenance", setup_calls)

        reminder_loop = _get_class_method(tree, class_name="NyctiBot", method_name="_run_reminder_poll_loop")
        reminder_calls = _collect_call_names(reminder_loop)
        self.assertIn("_run_retention_maintenance", reminder_calls)


if __name__ == "__main__":
    unittest.main()
