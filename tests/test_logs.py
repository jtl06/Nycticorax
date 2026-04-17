import unittest
from datetime import datetime, timedelta, timezone

from nycti.discord.logs import (
    RecentToolRow,
    ToolRow,
    UsageCategoryRow,
    UsageLogsSnapshot,
    UsageModelCategoryRow,
    UsageModelRow,
    _resolve_window,
    format_usage_logs_report,
)


class LogsFormattingTests(unittest.TestCase):
    def test_format_usage_logs_report_renders_models_features_and_tools(self) -> None:
        snapshot = UsageLogsSnapshot(
            usage_event_count=42,
            prompt_tokens=80000,
            completion_tokens=43456,
            total_tokens=123456,
            total_cost_usd=0.98765,
            context_prompt_tokens=32000,
            context_completion_tokens=4456,
            context_total_tokens=36456,
            model_rows=[
                UsageModelRow(
                    model="gpt-4.1-mini",
                    event_count=30,
                    prompt_tokens=70000,
                    completion_tokens=30000,
                    total_tokens=100000,
                    estimated_cost_usd=0.81234,
                )
            ],
            category_rows=[
                UsageCategoryRow(
                    category="chat_reply",
                    event_count=20,
                    prompt_tokens=60000,
                    completion_tokens=20000,
                    total_tokens=80000,
                )
            ],
            model_category_rows=[
                UsageModelCategoryRow(
                    model="gpt-4.1-mini",
                    category="chat_reply",
                    total_tokens=80000,
                )
            ],
            tool_rows=[
                ToolRow(
                    tool_name="web_search",
                    event_count=10,
                    ok_count=9,
                    error_count=1,
                    empty_count=0,
                    avg_latency_ms=740,
                )
            ],
            recent_tool_rows=[
                RecentToolRow(
                    tool_name="web_search",
                    status="ok",
                    latency_ms=712,
                    created_at=datetime(2026, 4, 17, 11, 59, tzinfo=timezone.utc),
                )
            ],
        )
        rendered = format_usage_logs_report(
            snapshot,
            scope="server",
            window_label="last 24h",
            now=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        )

        self.assertIn("Usage logs (`server`) for `last 24h`", rendered)
        self.assertIn("LLM events `42`", rendered)
        self.assertIn("Context bandwidth: total `36,456`", rendered)
        self.assertIn("`gpt-4.1-mini`", rendered)
        self.assertIn("`chat_reply`", rendered)
        self.assertIn("`web_search`", rendered)
        self.assertIn("1m ago", rendered)

    def test_format_usage_logs_report_handles_empty_sections(self) -> None:
        snapshot = UsageLogsSnapshot(
            usage_event_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            total_cost_usd=0.0,
            context_prompt_tokens=0,
            context_completion_tokens=0,
            context_total_tokens=0,
            model_rows=[],
            category_rows=[],
            model_category_rows=[],
            tool_rows=[],
            recent_tool_rows=[],
        )
        rendered = format_usage_logs_report(snapshot, scope="me", window_label="last 6h")

        self.assertIn("By model:", rendered)
        self.assertIn("By category (feature):", rendered)
        self.assertIn("By model + category:", rendered)
        self.assertIn("Tool calls:", rendered)
        self.assertIn("- (none)", rendered)

    def test_resolve_window_handles_reboot_and_custom(self) -> None:
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        started_at = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        reboot_since, reboot_label = _resolve_window(
            period="reboot",
            hours=None,
            now=now,
            started_at=started_at,
        )
        custom_since, custom_label = _resolve_window(
            period="custom",
            hours=36,
            now=now,
            started_at=started_at,
        )

        self.assertEqual(reboot_since, started_at)
        self.assertIn("since reboot", reboot_label)
        self.assertEqual(custom_since, now - timedelta(hours=36))
        self.assertEqual(custom_label, "last 36h")


if __name__ == "__main__":
    unittest.main()
