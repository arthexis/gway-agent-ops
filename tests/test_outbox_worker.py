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
        times = iter([0, 0, 0, 1, 1, 2, 2, 2, 2, 2])
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

    def test_invalid_batch_or_unsafe_lock_fails_closed(self):
        self.enqueue(1)
        with self.assertRaises(ValueError):
            flush(state_dir=self.root, max_reports=0)
        (self.root / "report.lock").symlink_to("outbox")
        with self.assertRaisesRegex(ValueError, "unsafe report lock"):
            flush(state_dir=self.root, runner=self.success)
        self.assertEqual(self.calls, [])
