import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location(
    "bounded_ab", Path(__file__).resolve().parents[1] / "scripts/run_bounded_market_ab.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BenchmarkSpendTests(unittest.TestCase):
    def test_reserves_before_request_and_preserves_unknown_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spend.json"
            path.write_text(json.dumps({"limit_usd": 0.2, "requests": []}))
            ledger = MODULE.SpendLedger(path, "test")
            request = {"model": "gpt-5.6-terra", "service_tier": "default", "max_output_tokens": 8192}
            index = ledger.reserve(request)
            ledger.finish(index, None, "TimeoutError", 10)
            with self.assertRaises(MODULE.BudgetStop):
                ledger.reserve(request)
            data = json.loads(path.read_text())
            self.assertEqual(data["requests"][0]["charged_usd"], data["requests"][0]["reserved_usd"])

    def test_settles_success_and_rejects_unpriced_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spend.json"
            path.write_text(json.dumps({"limit_usd": 2, "requests": []}))
            ledger = MODULE.SpendLedger(path, "test")
            request = {"model": "gpt-5.6-terra", "service_tier": "default", "max_output_tokens": 8192}
            index = ledger.reserve(request)
            ledger.finish(index, SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50)), "", 5)
            row = json.loads(path.read_text())["requests"][0]
            self.assertAlmostEqual(0.00085, row["charged_usd"])
            for extra in ({"model": "unknown"}, {"service_tier": "fast"},
                          {"previous_response_id": "response"}, {"max_output_tokens": 9000},
                          {"tools": [{"type": "web_search"}]}):
                with self.subTest(extra=extra), self.assertRaises(MODULE.BudgetStop):
                    ledger.reserve({**request, **extra})
