import json
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nycti.allocation_trace import AllocationTracer, TRACE_DURATION_SECONDS, TRACE_TOP_LIMIT


def _tracer_module():
    module = SimpleNamespace(__file__="/stdlib/tracemalloc.py", active=False)
    module.is_tracing = lambda: module.active
    module.start = Mock(side_effect=lambda frames: setattr(module, "active", True))
    module.stop = Mock(side_effect=lambda: setattr(module, "active", False))
    module.get_traced_memory = Mock(return_value=(1000, 2000))
    module.get_tracemalloc_memory = Mock(return_value=300)
    module.Filter = Mock()
    snapshot = Mock()
    snapshot.filter_traces.return_value = snapshot
    snapshot.compare_to.return_value = []
    module.take_snapshot = Mock(return_value=snapshot)
    return module


class AllocationTraceTests(unittest.TestCase):
    def test_session_is_bounded_and_repeated_start_does_not_extend_it(self):
        module = _tracer_module()
        loop = Mock()
        trace = AllocationTracer()
        with patch.dict(sys.modules, {"tracemalloc": module}), \
                patch("nycti.allocation_trace.asyncio.get_running_loop", return_value=loop), \
                patch("nycti.allocation_trace.LOGGER"):
            trace.start()
            trace.start()
            module.start.assert_called_once_with(1)
            loop.call_later.assert_called_once_with(TRACE_DURATION_SECONDS, trace.stop, "timeout")
            self.assertTrue(trace.status()["active"])
            callback = loop.call_later.call_args.args
            callback[1](*callback[2:])
            self.assertFalse(module.active)
            self.assertTrue(trace.status()["has_traced"])
            self.assertIsNone(trace.baseline)
            self.assertIsNone(trace.timer)
            loop.call_later.return_value.cancel.assert_called_once()

    def test_existing_external_tracing_is_never_stopped(self):
        module = _tracer_module()
        module.active = True
        trace = AllocationTracer()
        with patch.dict(sys.modules, {"tracemalloc": module}), patch("nycti.allocation_trace.LOGGER"):
            trace.start()
            trace.stop()
            trace.close()
            self.assertTrue(trace.status()["active"])
            self.assertFalse(trace.status()["owned"])
        module.start.assert_not_called()
        module.stop.assert_not_called()

    def test_start_failure_releases_owned_traces_and_hides_exception_details(self):
        module = _tracer_module()
        module.take_snapshot.side_effect = RuntimeError("SECRET")
        trace = AllocationTracer()
        with patch.dict(sys.modules, {"tracemalloc": module}), \
                patch("nycti.allocation_trace.asyncio.get_running_loop", return_value=Mock()), \
                patch("nycti.allocation_trace.LOGGER") as logger:
            trace.start()
        self.assertFalse(module.active)
        self.assertFalse(trace.owns_tracing)
        self.assertNotIn("SECRET", str(logger.mock_calls))

    def test_reports_are_bounded_metadata_only_and_rate_limited(self):
        module = _tracer_module()
        changes = [SimpleNamespace(size_diff=index, size=index * 2, count=2, count_diff=1,
                                   traceback=[SimpleNamespace(filename="/home/private-user/src/nycti/example.py", lineno=42)])
                   for index in range(1, 40)]
        module.take_snapshot.return_value.compare_to.return_value = changes
        trace = AllocationTracer()
        with patch.dict(sys.modules, {"tracemalloc": module}), \
                patch("nycti.allocation_trace.asyncio.get_running_loop", return_value=Mock()), \
                patch("nycti.allocation_trace.LOGGER") as logger:
            trace.start()
            trace.report()
            trace.report()
            payloads = [json.loads(call.args[1]) for call in logger.info.call_args_list]
            reports = [item for item in payloads if item["phase"] == "snapshot"]
            self.assertEqual(1, len(reports))
            self.assertEqual(TRACE_TOP_LIMIT, len(reports[0]["top_growth"]))
            self.assertEqual(39, reports[0]["top_growth"][0]["change_bytes"])
            self.assertNotIn("private-user", json.dumps(payloads))
            trace.close()

    def test_report_failure_still_stops_and_releases_baseline(self):
        module = _tracer_module()
        trace = AllocationTracer()
        with patch.dict(sys.modules, {"tracemalloc": module}), \
                patch("nycti.allocation_trace.asyncio.get_running_loop", return_value=Mock()), \
                patch("nycti.allocation_trace.LOGGER"):
            trace.start()
            module.take_snapshot.side_effect = RuntimeError("snapshot failure")
            trace.stop()
        self.assertFalse(module.active)
        self.assertIsNone(trace.baseline)

    def test_real_tracing_attributes_retained_allocation_without_logging_contents(self):
        script = textwrap.dedent("""
            import asyncio, io, json, logging, sys, tracemalloc
            sys.path.insert(0, 'src')
            from nycti.allocation_trace import AllocationTracer
            async def run():
                output = io.StringIO()
                logger = logging.getLogger('nycti.allocation_trace')
                logger.setLevel(logging.INFO)
                logger.propagate = False
                logger.addHandler(logging.StreamHandler(output))
                trace = AllocationTracer()
                assert not tracemalloc.is_tracing()
                try:
                    trace.start()
                    assert tracemalloc.is_tracing()
                    payload = bytearray(b'PRIVATE_ALLOCATION_CONTENT' * 10000)
                    trace.report()
                    lines = [json.loads(line.split(' ', 1)[1]) for line in output.getvalue().splitlines()]
                    snapshot = next(item for item in lines if item['phase'] == 'snapshot')
                    assert any(item['change_bytes'] >= len(payload) for item in snapshot['top_growth'])
                    assert 'PRIVATE_ALLOCATION_CONTENT' not in output.getvalue()
                    trace.stop()
                    assert trace.baseline is None
                    assert not tracemalloc.is_tracing()
                finally:
                    trace.close()
            asyncio.run(run())
            print('ok')
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", result.stdout.strip())
