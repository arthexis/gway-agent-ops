"""Bounded idempotent report retries; no network or Codex subprocess."""
import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.reporting import enqueue, flush


class OutboxWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []

    def enqueue(self, i):
        return enqueue(f"arthexis/example#{i}", "2026-10-13T15:00:00.000000Z",
                       state_dir=self.root)

    def success(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "per_page=100" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def test_bounded_batch_resumes_on_next_tick_without_relaunch(self):
        for i in range(1, 6):
            self.enqueue(i)
        self.assertEqual(flush(state_dir=self.root, runner=self.success, max_reports=2),
                         {"sent": 2, "pending": 3})
        self.assertEqual(flush(state_dir=self.root, runner=self.success, max_reports=2),
                         {"sent": 2, "pending": 1})
        self.assertEqual(flush(state_dir=self.root, runner=self.success, max_reports=2),
                         {"sent": 1, "pending": 0})
        self.assertEqual(flush(state_dir=self.root, runner=self.success),
                         {"sent": 0, "pending": 0})
        self.assertEqual(len(list((self.root / "delivered").glob("*.json"))), 5)
        self.assertTrue(all(args[0][:2] == ["gh", "api"] for args in self.calls))
        self.assertEqual(sum("body=" in " ".join(args[0]) for args in self.calls), 5)

    def test_busy_reporter_does_not_call_github(self):
        self.enqueue(1)
        fd = os.open(self.root / "report.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(flush(state_dir=self.root, runner=self.success),
                             {"sent": 0, "pending": 1})
            self.assertEqual(self.calls, [])
        finally:
            os.close(fd)
        self.assertEqual(flush(state_dir=self.root, runner=self.success),
                         {"sent": 1, "pending": 0})

    def test_time_budget_keeps_queued_records(self):
        self.enqueue(1)
        self.enqueue(2)
        times = iter([0, 1, 1])
        def clock():
            return next(times, 2)
        self.assertEqual(flush(state_dir=self.root, runner=self.success,
                               max_reports=4, budget_seconds=1, clock=clock),
                         {"sent": 0, "pending": 2})
        self.assertEqual(self.calls, [])
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 2)

    def test_failed_github_delivery_survives_for_next_tick(self):
        self.enqueue(1)
        def failing(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 1, "", "")
        self.assertEqual(flush(state_dir=self.root, runner=failing),
                         {"sent": 0, "pending": 1})
        self.assertEqual(flush(state_dir=self.root, runner=self.success),
                         {"sent": 1, "pending": 0})
        self.assertEqual(flush(state_dir=self.root, runner=self.success),
                         {"sent": 0, "pending": 0})

    def test_poison_prefix_does_not_starve_valid_report(self):
        records = [self.enqueue(i) for i in range(1, 6)]
        filenames = sorted((self.root / "outbox").glob("*.json"))
        good = json.loads(filenames[-1].read_text())["issue"]
        calls = []
        def mixed_runner(argv, **kwargs):
            calls.append(argv)
            if good.split("#")[1] not in argv[2].split("/issues/")[-1].split("/")[0]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            if "per_page=100" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        self.assertEqual(flush(state_dir=self.root, runner=mixed_runner, max_reports=4),
                         {"sent": 0, "pending": 5})
        self.assertEqual(flush(state_dir=self.root, runner=mixed_runner, max_reports=4),
                         {"sent": 1, "pending": 4})
        self.assertFalse((self.root / "outbox" / filenames[-1].name).exists())
        self.assertTrue((self.root / "delivered" / filenames[-1].name).exists())
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 4)
        self.assertTrue((self.root / "report-cursor.json").exists())

    def test_cursor_rejects_symlink_or_invalid_state(self):
        self.enqueue(1)
        cursor = self.root / "report-cursor.json"
        cursor.symlink_to("outbox")
        with self.assertRaisesRegex(ValueError, "unsafe report cursor"):
            flush(state_dir=self.root, runner=self.success)
        cursor.unlink()
        cursor.write_text('{"last":"../../untrusted.json"}')
        with self.assertRaisesRegex(ValueError, "invalid report cursor"):
            flush(state_dir=self.root, runner=self.success)
        self.assertEqual(self.calls, [])

    def test_only_declarative_timer_invokes_outbox_command(self):
        units = Path(__file__).resolve().parent.parent / "systemd"
        service = (units / "gway-agent-report.service").read_text()
        timer = (units / "gway-agent-report.timer").read_text()
        self.assertIn("Type=oneshot", service)
        commands = [line for line in service.splitlines()
                    if line.startswith("ExecStart=")]
        self.assertEqual(commands, ["ExecStart=%h/.local/bin/agent report --json"])
        self.assertIn("TimeoutStartSec=120", service)
        self.assertIn("OnCalendar=*:0/5", timer)
        self.assertIn("Unit=gway-agent-report.service", timer)
        self.assertNotIn("ExecStart=", timer)
        self.assertNotIn("enable", service + timer)
        self.assertNotIn("ExecStartPre=", service)

    def test_invalid_batch_or_unsafe_lock_fails_closed(self):
        self.enqueue(1)
        with self.assertRaises(ValueError):
            flush(state_dir=self.root, max_reports=0)
        (self.root / "report.lock").symlink_to("outbox")
        with self.assertRaisesRegex(ValueError, "unsafe report lock"):
            flush(state_dir=self.root, runner=self.success)
        self.assertEqual(self.calls, [])
