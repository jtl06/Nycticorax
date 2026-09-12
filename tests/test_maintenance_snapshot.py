from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import MagicMock

from scripts.collect_maintenance_snapshot import collect_snapshot, write_snapshot


class MaintenanceSnapshotTests(unittest.TestCase):
    def test_queries_are_scoped_reads_and_bundles_are_opt_in(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [
            [{"feedback_message_id": 1, "bundle": "private trace", "feedback_text": "bad bot"}],
            [], [], [], [], [],
        ]
        snapshot = collect_snapshot(connection, guild_id=42, hours=24, limit=10)
        self.assertNotIn("bundle", snapshot["incidents"][0])
        for call in cursor.execute.call_args_list:
            query, params = call.args
            self.assertTrue(query.startswith("SELECT "))
            self.assertIn("guild_id = %s", query)
            self.assertIn(42, params)

    def test_raw_bundle_is_unchanged_when_requested(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        raw = "untrusted text\n{\"raw\": [1, 2]}"
        cursor.fetchall.side_effect = [[{"bundle": raw}], [], [], [], [], []]
        snapshot = collect_snapshot(connection, guild_id=42, hours=24, limit=10, include_bundles=True)
        self.assertEqual(snapshot["incidents"][0]["bundle"], raw)

    def test_report_is_private_and_cannot_escape_ignored_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / ".local/maintenance/report.json"
            write_snapshot({"incidents": []}, output, root=root)
            self.assertEqual(json.loads(output.read_text()), {"incidents": []})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                write_snapshot({}, root / "public.json", root=root)
            outside = root / "outside"
            outside.mkdir()
            (output.parent / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                write_snapshot({}, output.parent / "escape/leak.json", root=root)


if __name__ == "__main__":
    unittest.main()
