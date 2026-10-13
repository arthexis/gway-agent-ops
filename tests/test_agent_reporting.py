"""Durable, idempotent A3.4 reporting and crash recovery."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.reporting import enqueue, flush, recover


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dispatch"
        self.issue = "arthexis/example#19"
        self.at = "2026-10-13T06:00:00.000000Z"

    def test_report_delivery_is_durable_and_sanitized(self):
        record = enqueue(self.issue, self.at, state_dir=self.root, status="codex_failed", exit_code=1)
        self.assertNotIn("secret", json.dumps(record))
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if "--slurp" in argv:
                return subprocess.CompletedProcess(argv, 0, "[[]]", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        self.assertEqual(flush(state_dir=self.root, runner=runner), {"sent": 1, "pending": 0})
        self.assertTrue(any("codex_failed" in str(argv) for argv in calls))
        self.assertEqual(flush(state_dir=self.root, runner=runner), {"sent": 0, "pending": 0})
        self.assertEqual(len(list((self.root / "delivered").glob("*.json"))), 1)

    def test_github_failure_keeps_report_for_retry(self):
        enqueue(self.issue, self.at, state_dir=self.root)
        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "")
        self.assertEqual(flush(state_dir=self.root, runner=failing), {"sent": 0, "pending": 1})
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 1)

    def test_existing_remote_marker_prevents_reposting(self):
        record = enqueue(self.issue, self.at, state_dir=self.root)
        def runner(argv, **kwargs):
            if "--slurp" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps([[{"body": record["marker"]}]]), "")
            self.fail("a duplicate GitHub comment was posted")
        self.assertEqual(flush(state_dir=self.root, runner=runner)["sent"], 1)

    def test_recover_only_old_dispatches_and_not_delivered(self):
        self.root.mkdir(mode=0o700)
        ledger = self.root / "accepted.jsonl"
        ledger.write_text(json.dumps({"issue": self.issue, "at": self.at}) + "\n")
        ledger.chmod(0o600)
        self.assertEqual(recover(state_dir=self.root, active=(self.issue, self.at)), 0)
        self.assertEqual(recover(state_dir=self.root), 1)
        self.assertEqual(recover(state_dir=self.root), 0)

    def test_other_issue_same_timestamp_is_distinct(self):
        a = enqueue(self.issue, self.at, state_dir=self.root)
        b = enqueue("arthexis/other#20", self.at, state_dir=self.root)
        self.assertNotEqual(a["marker"], b["marker"])
