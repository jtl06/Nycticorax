"""Prevent real Discord snowflakes from returning to public benchmark fixtures."""
from pathlib import Path
import re
import unittest


class BenchmarkPrivacyTests(unittest.TestCase):
    def test_public_benchmark_ids_use_reserved_synthetic_namespace(self):
        paths = [*Path("benchmarks").rglob("*.json"), *Path("benchmarks").rglob("*.md"),
                 *Path(".").glob("benchmark*.md"), Path("example_prompt.md")]
        offenders = []
        for path in paths:
            ids = re.findall(r"(?<!\d)\d{17,20}(?!\d)", path.read_text())
            if any(not value.startswith("900000") for value in ids):
                offenders.append(str(path))
        self.assertEqual(offenders, [])
