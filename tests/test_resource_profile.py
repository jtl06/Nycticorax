from datetime import datetime, timedelta, timezone
import json
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nycti.resource_profile import (
    PROFILE_HISTORY_LIMIT,
    ResourceProfiler, collect_resource_profile, request_resource_profile, resource_profile_hook,
)
from nycti.config import ConfigurationError, Settings
from nycti.resource_metrics import allocator_counters, runtime_counters


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
    def test_bounded_history_separates_idle_from_busy_and_omits_missing_metrics(self):
        profiler = ResourceProfiler(_bot())

        def sample(rss, active=False):
            return {"timestamp_utc": "2026-09-14T00:00:00Z", "process": {"VmRSS_bytes": rss},
                    "active_requests": 0, "queues": {"memory": {"active": active, "pending": 0}}}

        with patch("nycti.resource_profile.collect_resource_profile", side_effect=[sample(100), sample(900, True), sample(150)]):
            for now in (0, 300, 600):
                profiler._capture(now)
        summary = profiler.summary()
        self.assertEqual(900, summary["rss"]["peak_bytes"])
        self.assertEqual(150, summary["idle_rss"]["peak_bytes"])
        self.assertEqual(50, summary["idle_rss"]["change_bytes"])
        self.assertEqual(600, summary["idle_rss"]["window_seconds"])
        with patch("nycti.resource_profile.collect_resource_profile", return_value={"pid": 1}):
            for now in range(PROFILE_HISTORY_LIMIT + 5):
                profiler._capture(900 + now)
        summary = profiler.summary()
        self.assertEqual(PROFILE_HISTORY_LIMIT, summary["sample_count"])
        self.assertEqual(6, len(summary["recent"]))
        self.assertNotIn("rss", summary)
        self.assertNotIn("idle_rss", summary)

    def test_traced_samples_are_excluded_from_normal_idle_comparison(self):
        profiler = ResourceProfiler(_bot())
        profile = {"process": {"VmRSS_bytes": 100}, "active_requests": 0,
                   "queues": {"memory": {"active": False, "pending": 0}}}
        with patch("nycti.resource_profile.collect_resource_profile", return_value=profile), \
                patch.object(profiler.allocations, "status", side_effect=[{"active": False}, {"active": True}, {"active": False, "has_traced": True}]):
            profiler._capture(0)
            profile["process"]["VmRSS_bytes"] = 500
            profiler._capture(300)
            profile["process"]["VmRSS_bytes"] = 300
            profiler._capture(600)
        summary = profiler.summary()
        self.assertEqual(100, summary["idle_rss"]["peak_bytes"])
        self.assertEqual(500, summary["traced_rss"]["peak_bytes"])
        self.assertEqual(300, summary["post_trace_rss"]["peak_bytes"])

    def test_sampling_obeys_interval_and_can_be_disabled_without_disabling_manual_profiles(self):
        bot = _bot()
        bot.settings = SimpleNamespace(resource_profile_interval_seconds=300)
        profiler = ResourceProfiler(bot)
        with patch("nycti.resource_profile.time.monotonic", side_effect=[0, 60, 300]), \
                patch("nycti.resource_profile.collect_resource_profile", return_value={"pid": 1}) as collect, \
                patch("nycti.resource_profile.LOGGER"):
            for _ in range(3):
                profiler.sample_if_due()
        self.assertEqual(2, collect.call_count)
        bot.settings.resource_profile_interval_seconds = 0
        with patch("nycti.resource_profile.collect_resource_profile", return_value={"pid": 1}) as collect, \
                patch("nycti.resource_profile.LOGGER"):
            profiler.sample_if_due()
            collect.assert_not_called()
            profiler.emit()
            collect.assert_called_once()

    def test_failed_periodic_capture_is_rate_limited_and_does_not_leak(self):
        profiler = ResourceProfiler(_bot())
        with patch("nycti.resource_profile.time.monotonic", side_effect=[0, 60]), \
                patch("nycti.resource_profile.collect_resource_profile", side_effect=RuntimeError("SECRET")) as collect, \
                patch("nycti.resource_profile.LOGGER") as logger:
            profiler.sample_if_due()
            profiler.sample_if_due()
        self.assertEqual(1, collect.call_count)
        self.assertEqual(0, len(profiler.history))
        self.assertNotIn("SECRET", str(logger.mock_calls))

    def test_native_allocator_reports_only_counters_or_unavailability(self):
        with patch("nycti.resource_metrics._allocator_reader", return_value=None):
            self.assertEqual({}, allocator_counters())
        with patch("nycti.resource_metrics._allocator_reader", return_value=lambda: SimpleNamespace(
            arena=100, uordblks=70, fordblks=30, hblkhd=10,
        )):
            self.assertEqual({"arena_bytes": 100, "in_use_bytes": 70, "free_bytes": 30, "mmap_bytes": 10}, allocator_counters())

    def test_resource_sampling_config_is_bounded(self):
        env = {"DISCORD_TOKEN": "test", "OPENAI_API_KEY": "test", "DATABASE_URL": "sqlite:///test.db"}
        self.assertEqual(300, Settings.from_env(env).resource_profile_interval_seconds)
        for value in (0, 60, 300, 3600):
            self.assertEqual(value, Settings.from_env({**env, "RESOURCE_PROFILE_INTERVAL_SECONDS": str(value)}).resource_profile_interval_seconds)
        for value in (-1, 1, 59, 3601):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Settings.from_env({**env, "RESOURCE_PROFILE_INTERVAL_SECONDS": str(value)})

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
        expected = {signal.SIGUSR1, signal.SIGUSR2}
        if hasattr(signal, "SIGRTMIN"):
            expected.add(signal.SIGRTMIN)
        self.assertEqual(expected, {call.args[0] for call in loop.add_signal_handler.call_args_list})
        self.assertEqual(expected, {call.args[0] for call in loop.remove_signal_handler.call_args_list})
        for number in expected:
            restore.assert_any_call(number, signal.SIG_DFL)

    @unittest.skipUnless(hasattr(signal, "SIGUSR2"), "Unix signals required")
    def test_trace_start_requires_its_own_registered_signal(self):
        with patch("nycti.resource_profile._read", return_value="python\0-m\0nycti.main\0"), \
                patch("nycti.resource_profile._status", return_value={"SigCgt": f"{1 << (signal.SIGUSR1 - 1):x}"}), \
                patch("nycti.resource_profile.os.kill") as kill:
            with self.assertRaises(ValueError):
                request_resource_profile(1, trace_action="start")
            kill.assert_not_called()
        with patch("nycti.resource_profile._read", return_value="python\0-m\0nycti.main\0"), \
                patch("nycti.resource_profile._status", return_value={"SigCgt": f"{1 << (signal.SIGUSR2 - 1):x}"}), \
                patch("nycti.resource_profile.os.kill") as kill:
            request_resource_profile(1, trace_action="start")
            kill.assert_called_once_with(1, signal.SIGUSR2)

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "Unix signals required")
    def test_unsupported_loop_keeps_bot_running(self):
        loop = Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        with patch("nycti.resource_profile.asyncio.get_running_loop", return_value=loop):
            with resource_profile_hook(_bot()):
                pass
        loop.remove_signal_handler.assert_not_called()


class RuntimeCountersTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_observation_does_not_create_an_executor(self):
        import asyncio

        loop = asyncio.get_running_loop()
        self.assertIsNone(loop._default_executor)
        counters = runtime_counters()
        self.assertEqual(0, counters["executor_threads"])
        self.assertIsNone(loop._default_executor)
        await asyncio.to_thread(lambda: None)
        self.assertGreaterEqual(runtime_counters()["executor_threads"], 1)
