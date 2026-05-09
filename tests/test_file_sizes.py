from pathlib import Path
import subprocess
import unittest


class FileSizeTests(unittest.TestCase):
    def test_tracked_files_stay_under_1000_lines(self) -> None:
        result = subprocess.run(
            ["git", "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        )
        oversized: list[str] = []
        for filename in result.stdout.splitlines():
            path = Path(filename)
            if not path.is_file():
                continue
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count > 1000:
                oversized.append(f"{filename}: {line_count}")

        self.assertEqual([], oversized)


if __name__ == "__main__":
    unittest.main()
