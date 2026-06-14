from pathlib import Path
import unittest


class FileSizeTests(unittest.TestCase):
    def test_source_files_stay_under_emergency_1200_line_ceiling(self) -> None:
        oversized: list[str] = []
        paths = (
            *Path("src").rglob("*.py"),
            *Path("tests").rglob("*.py"),
            *Path("scripts").rglob("*.py"),
        )
        for path in paths:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count > 1200:
                oversized.append(f"{path}: {line_count}")

        self.assertEqual([], oversized)

    def test_core_orchestrator_stays_within_target_range(self) -> None:
        line_count = len(Path("src/nycti/chat/orchestrator.py").read_text().splitlines())

        self.assertLessEqual(line_count, 400)


if __name__ == "__main__":
    unittest.main()
