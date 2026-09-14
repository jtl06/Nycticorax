from datetime import datetime, timedelta, timezone
import json
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nycti.resource_profile import (
    ResourceProfiler, collect_resource_profile, request_resource_profile, resource_profile_hook,
)


def _bot():
    job = SimpleNamespace(pending_count=2, queue=SimpleNamespace(maxsize=64), task=None)
    sensitive = "SECRET private message and credentials"
    snapshot = SimpleNamespace(
        captured_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        prompt=sensitive, reply_text=sensitive, context_lines=(sensitive,), image_context_lines=(),
        metrics={"debug": sensitive, "count": 1},
    )
    return SimpleNamespace(
        started_at_utc=datetime.now(timezone.utc) - timedelta(hours=1),
        cached_messages=[SimpleNamespace(content=sensitive)],
        _connection=SimpleNamespace(max_messages=1000),
        _response_diagnostic_cache=SimpleNamespace(_snapshots=[snapshot], max_entries=24, max_age=timedelta(minutes=15)),
        _background_memory_writer=SimpleNamespace(_jobs=job),
        _background_procedure_learner=None,
        _emoji_learner=SimpleNamespace(jobs=job, evidence={}),
        _chat_orchestrator=SimpleNamespace(telemetry_writer=SimpleNamespace(_jobs=job)),
        _active_requests=SimpleNamespace(_tasks={}),
        database=SimpleNamespace(engine=SimpleNamespace(pool=SimpleNamespace(size=lambda: 5, checkedin=lambda: 4))),
    )


class ResourceProfileTests(unittest.TestCase):
    def test_profile_exposes_counts_not_contents_and_does_not_prune(self):
        bot = _bot()
        with patch("nycti.resource_profile._read", return_value=""):
            profile = collect_resource_profile(bot)
        self.assertNotIn("SECRET", json.dumps(profile))
        self.assertEqual({}, profile["process"])
        self.assertEqual({}, profile["container"])
        self.assertEqual(1, profile["discord_cache"]["messages"])
        self.assertGreater(profile["discord_cache"]["text_shallow_bytes"], 0)
        self.assertEqual(1, profile["response_diagnostics"]["expired_entries"])
        self.assertEqual(1, len(bot._response_diagnostic_cache._snapshots))
        self.assertEqual(2, profile["queues"]["memory"]["pending"])
        self.assertEqual({"size": 5, "checkedin": 4}, profile["database_pool"])

    def test_linux_counters_use_bytes_and_preserve_container_separation(self):
        paths = {"/proc/self/status": "VmRSS:\t100 kB\nThreads:\t5\n",
                 "/sys/fs/cgroup/memory.current": "200000\n",
                 "/sys/fs/cgroup/memory.stat": "anon 120000\nfile 70000\nshmem 1000\n"}
        with patch("nycti.resource_profile._read", side_effect=lambda path: paths.get(str(path), "")):
            profile = collect_resource_profile(_bot())
        self.assertEqual({"VmRSS_bytes": 102400, "threads": 5}, profile["process"])
        self.assertEqual(200000, profile["container"]["memory_current_bytes"])

    def test_profile_requests_are_cooled_down_and_errors_do_not_leak(self):
        profiler = ResourceProfiler(_bot())
        with patch("nycti.resource_profile.time.monotonic", side_effect=[1, 2, 16]), \
                patch("nycti.resource_profile.collect_resource_profile", return_value={"pid": 1}) as collect, \
                patch("nycti.resource_profile.LOGGER") as logger:
            profiler.emit()
            profiler.emit()
            profiler.emit()
        self.assertEqual(2, collect.call_count)
        self.assertEqual(2, logger.info.call_count)
        with patch("nycti.resource_profile.collect_resource_profile", side_effect=RuntimeError("SECRET")), \
                patch("nycti.resource_profile.LOGGER") as logger:
            ResourceProfiler(_bot()).emit()
        self.assertNotIn("SECRET", str(logger.mock_calls))

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "Unix signals required")
    def test_cli_only_signals_a_matching_process_with_handler(self):
        bit = 1 << (signal.SIGUSR1 - 1)
        for command, caught, allowed in (
            ("python\0-m\0nycti.main\0", f"{bit:x}", True),
            ("python\0-m\0nycti.main\0", "0", False),
            ("python\0other.py\0", f"{bit:x}", False),
        ):
            with self.subTest(command=command, caught=caught), \
                    patch("nycti.resource_profile._read", return_value=command), \
                    patch("nycti.resource_profile._status", return_value={"SigCgt": caught}), \
                    patch("nycti.resource_profile.os.kill") as kill:
                if allowed:
                    request_resource_profile(1)
                    kill.assert_called_once_with(1, signal.SIGUSR1)
                else:
                    with self.assertRaises(ValueError):
                        request_resource_profile(1)
                    kill.assert_not_called()
        with patch("nycti.resource_profile.os.kill") as kill:
            for pid in (-1, 0):
                with self.assertRaises(ValueError):
                    request_resource_profile(pid)
            kill.assert_not_called()

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "Unix signals required")
    def test_hook_restores_signal_handler_on_exit(self):
        loop = Mock()
        with patch("nycti.resource_profile.asyncio.get_running_loop", return_value=loop), \
                patch("nycti.resource_profile.signal.getsignal", return_value=signal.SIG_DFL), \
                patch("nycti.resource_profile.signal.signal") as restore:
            with self.assertRaises(RuntimeError):
                with resource_profile_hook(_bot()):
                    raise RuntimeError("shutdown")
        loop.add_signal_handler.assert_called_once()
        loop.remove_signal_handler.assert_called_once_with(signal.SIGUSR1)
        restore.assert_called_once_with(signal.SIGUSR1, signal.SIG_DFL)

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "Unix signals required")
    def test_unsupported_loop_keeps_bot_running(self):
        loop = Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        with patch("nycti.resource_profile.asyncio.get_running_loop", return_value=loop):
            with resource_profile_hook(_bot()):
                pass
        loop.remove_signal_handler.assert_not_called()
