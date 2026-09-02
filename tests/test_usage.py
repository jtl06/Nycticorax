import ast
import asyncio
from pathlib import Path
import unittest

from nycti.usage import build_additive_timing_metrics, record_message_debug_stats


class UsageSourceTests(unittest.TestCase):
    def test_context_profile_timings_are_persisted_with_additive_totals(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.events = []

            def add_all(self, events) -> None:  # type: ignore[no-untyped-def]
                self.events.extend(events)

            async def flush(self) -> None:
                return None

        session = FakeSession()
        asyncio.run(
            record_message_debug_stats(
                session,  # type: ignore[arg-type]
                metrics={
                    "end_to_end_ms": 1000,
                    "context_fetch_ms": 100,
                    "ctx_recent_ms": 60,
                    "ctx_reply_ms": 20,
                    "ctx_prompt_ms": 2,
                },
                guild_id=1,
                channel_id=2,
                user_id=3,
                source_message_id=4,
            )
        )

        parts = {event.part for event in session.events}
        self.assertIn("timing_total_ms", parts)
        self.assertIn("ctx_recent_ms", parts)
        self.assertIn("ctx_reply_ms", parts)
        self.assertIn("ctx_prompt_ms", parts)

    def test_additive_timing_metrics_partition_end_to_end_wall_time(self) -> None:
        phases = build_additive_timing_metrics(
            {
                "end_to_end_ms": 10_000,
                "context_fetch_ms": 400,
                "reply_generation_ms": 9_200,
                "routing_latency_ms": 9_500,
                "chat_llm_ms": 5_000,
                "tool_execution_wall_ms": 2_000,
                "web_search_ms": 1_700,
                "stock_quote_ms": 1_200,
                "memory_retrieval_ms": 500,
                "vision_wait_ms": 100,
                "chat_commit_ms": 20,
                "reply_send_ms": 300,
            }
        )

        self.assertEqual(10_000, phases["timing_total_ms"])
        self.assertEqual(2_000, phases["timing_tools_ms"])
        self.assertNotIn("routing_latency_ms", phases)
        self.assertNotIn("web_search_ms", phases)
        self.assertEqual(
            phases["timing_total_ms"],
            sum(
                value
                for key, value in phases.items()
                if key != "timing_total_ms"
            ),
        )

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
