import ast
from pathlib import Path
import unittest


class UsageSourceTests(unittest.TestCase):
    def test_usage_prune_function_exists_and_deletes_old_rows(self) -> None:
        source = Path("src/nycti/usage.py").read_text()
        tree = ast.parse(source)

        function_nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
        prune_fn = next((node for node in function_nodes if node.name == "prune_usage_events_before"), None)
        self.assertIsNotNone(prune_fn)
        assert prune_fn is not None

        uses_delete = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "delete"
            for node in ast.walk(prune_fn)
        )
        self.assertTrue(uses_delete)

        compares_created_at = any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "created_at"
            and any(isinstance(op, ast.Lt) for op in node.ops)
            for node in ast.walk(prune_fn)
        )
        self.assertTrue(compares_created_at)

    def test_message_debug_stats_record_and_prune_functions_exist(self) -> None:
        source = Path("src/nycti/usage.py").read_text()
        tree = ast.parse(source)
        function_nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]

        record_fn = next((node for node in function_nodes if node.name == "record_message_debug_stats"), None)
        prune_fn = next((node for node in function_nodes if node.name == "prune_message_debug_events_before"), None)

        self.assertIsNotNone(record_fn)
        self.assertIsNotNone(prune_fn)
        assert record_fn is not None
        assert prune_fn is not None

        records_ms_metrics = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "endswith"
            and any(isinstance(arg, ast.Constant) and arg.value == "_ms" for arg in node.args)
            for node in ast.walk(record_fn)
        )
        self.assertTrue(records_ms_metrics)

        deletes_debug_events = any(
            isinstance(node, ast.Name)
            and node.id == "MessageDebugEvent"
            for node in ast.walk(prune_fn)
        )
        self.assertTrue(deletes_debug_events)

    def test_agent_and_action_retention_paths_delete_durable_rows(self) -> None:
        tree = ast.parse(Path("src/nycti/usage.py").read_text())
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }

        agent_prune = functions["prune_agent_telemetry_before"]
        action_prune = functions["prune_action_idempotency_before"]
        agent_models = {
            node.id
            for node in ast.walk(agent_prune)
            if isinstance(node, ast.Name)
        }
        action_attributes = {
            node.attr
            for node in ast.walk(action_prune)
            if isinstance(node, ast.Attribute)
        }

        self.assertTrue({"AgentRunEvent", "AgentStepEvent", "ToolCallEvent"} <= agent_models)
        self.assertIn("updated_at", action_attributes)
        self.assertIn("like", action_attributes)


if __name__ == "__main__":
    unittest.main()
