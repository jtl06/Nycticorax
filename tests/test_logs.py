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
            context_prompt_tokens=32000,
            context_completion_tokens=4456,
            context_total_tokens=36456,
            model_rows=[
                UsageModelRow(
                    model="https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5",
                    event_count=30,
                    prompt_tokens=70000,
                    completion_tokens=30000,
                    total_tokens=100000,
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
                    model="https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5",
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
            window_label="last 24h",
            now=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        )

        self.assertIn("Usage logs for `last 24h`", rendered)
        self.assertIn("LLM events `42`", rendered)
        self.assertIn("Context bandwidth: total `36,456`", rendered)
        self.assertIn("`clarifai kimi-k2.5`", rendered)
        self.assertNotIn("https://clarifai.com/moonshotai/chat-completion/models/Kimi-K2_5", rendered)
        self.assertIn("`chat_reply`", rendered)
        self.assertIn("`web_search`", rendered)
        self.assertIn("1m ago", rendered)

    def test_format_usage_logs_report_handles_empty_sections(self) -> None:
        snapshot = UsageLogsSnapshot(
            usage_event_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            context_prompt_tokens=0,
            context_completion_tokens=0,
            context_total_tokens=0,
            model_rows=[],
            category_rows=[],
            model_category_rows=[],
            tool_rows=[],
            recent_tool_rows=[],
        )
        rendered = format_usage_logs_report(snapshot, window_label="last 6h")

        self.assertIn("By model:", rendered)
        self.assertIn("By category (feature):", rendered)
        self.assertIn("By model + category:", rendered)
        self.assertIn("Tool calls:", rendered)
        self.assertIn("- (none)", rendered)

    def test_resolve_window_handles_week_and_custom(self) -> None:
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        week_since, week_label = _resolve_window(period="week", hours=None, now=now)
        custom_since, custom_label = _resolve_window(
            period="custom",
            hours=36,
            now=now,
        )

        self.assertEqual(week_since, now - timedelta(days=7))
        self.assertEqual(week_label, "last 7d")
        self.assertEqual(custom_since, now - timedelta(hours=36))
        self.assertEqual(custom_label, "last 36h")


if __name__ == "__main__":
    unittest.main()
