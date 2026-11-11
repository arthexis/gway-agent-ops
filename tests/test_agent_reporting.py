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

    def test_validated_findings_are_persisted_and_delivered(self):
        findings = {"summary": "The repository provides a local agent runner.",
                    "findings": ["The README describes the agent's purpose."],
                    "evidence": ["README.md contains the project overview."],
                    "limitations": "Only the README was examined."}
        record = enqueue(self.issue, self.at, state_dir=self.root,
                         status="finished", exit_code=0,
                         findings=findings, findings_state="validated")
        self.assertEqual(record["findings"], findings)
        self.assertEqual(record["findings_state"], "validated")
        self.assertEqual(record["admin_work_state"], "not_attested")
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if "per_page=100" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        self.assertEqual(flush(state_dir=self.root, runner=runner),
                         {"sent": 1, "pending": 0})
        self.assertTrue(any("The repository provides" in str(argv) for argv in calls))
        delivered = next((self.root / "delivered").glob("*.json"))
        receipt = json.loads(delivered.read_text())
        self.assertEqual(receipt["findings_state"], "validated")
        self.assertEqual(receipt["admin_work_state"], "not_attested")
        self.assertTrue(any("External services" in str(argv) for argv in calls))

    def test_missing_findings_are_explicitly_reported(self):
        record = enqueue(self.issue, self.at, state_dir=self.root,
                         status="finished", exit_code=0,
                         findings_state="missing_or_invalid")
        self.assertEqual(record["findings_state"], "missing_or_invalid")
        self.assertNotIn("findings", record)

    def test_report_delivery_is_durable_and_sanitized(self):
        record = enqueue(self.issue, self.at, state_dir=self.root, status="codex_failed", exit_code=1)
        self.assertNotIn("secret", json.dumps(record))
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if "per_page=100" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, "[]", "")
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
            if "per_page=100" in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"body": record["marker"]}]), "")
            self.fail("a duplicate GitHub comment was posted")
        self.assertEqual(flush(state_dir=self.root, runner=runner)["sent"], 1)

    def test_marker_on_later_page_prevents_duplicate(self):
        record = enqueue(self.issue, self.at, state_dir=self.root)
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[2].endswith("&page=1"):
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"body": "other"}] * 100), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"body": record["marker"]}]), "")
        self.assertEqual(flush(state_dir=self.root, runner=runner), {"sent": 1, "pending": 0})
        self.assertEqual(len(calls), 2)

    def test_recover_never_auto_reconciles_accepted_orphans(self):
        from gway_agent_ops.admission_status import status
        from gway_agent_ops.dispatch import Busy, admission
        from gway_agent_ops.observation import observe
        self.root.mkdir(mode=0o700)
        ledger = self.root / "accepted.jsonl"
        ledger.write_text(json.dumps({"issue": self.issue, "at": self.at}) + "\n")
        ledger.chmod(0o600)
        for active in ((self.issue, self.at), None, None):
            self.assertEqual(recover(state_dir=self.root, active=active), 0)
        self.assertFalse((self.root / "outbox").exists())
        self.assertFalse((self.root / "delivered").exists())
        self.assertEqual(observe(self.issue, self.at, state_dir=self.root)["state"], "accepted")
        self.assertFalse(status(state_dir=self.root)["available"])
        with self.assertRaises(Busy):
            with admission("arthexis/other#20", self.at, state_dir=self.root):
                pass

    def test_recover_rejects_corrupt_journal_and_symlink_without_writes(self):
        self.root.mkdir(mode=0o700)
        ledger = self.root / "accepted.jsonl"
        for item in ("[]", "null", '{"issue": 12, "at": "x"}',
                     '{"issue": "invalid", "at": "x"}', '{"issue":'):
            with self.subTest(item=item):
                ledger.write_text(item + "\n")
                with self.assertRaises(ValueError):
                    recover(state_dir=self.root)
                self.assertFalse((self.root / "outbox").exists())
        ledger.unlink()
        ledger.symlink_to("missing-ledger")
        with self.assertRaisesRegex(ValueError, "unsafe dispatch journal"):
            recover(state_dir=self.root)
        self.assertFalse((self.root / "outbox").exists())

    def test_wrong_identity_report_cannot_free_admission_or_post(self):
        from gway_agent_ops.admission_status import status
        from gway_agent_ops.observation import observe
        from gway_agent_ops.reporting import _key
        self.root.mkdir(mode=0o700)
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": self.issue, "at": self.at}) + "\n")
        enqueue(self.issue, self.at, state_dir=self.root, status="finished",
                exit_code=0)
        path = self.root / "outbox" / (_key(self.issue, self.at) + ".json")
        wrong = json.loads(path.read_text())
        wrong["issue"] = "arthexis/unrelated#99"
        path.write_text(json.dumps(wrong))
        with self.assertRaisesRegex(ValueError, "invalid report identity"):
            observe(self.issue, self.at, state_dir=self.root)
        with self.assertRaisesRegex(ValueError, "invalid report identity"):
            enqueue(self.issue, self.at, state_dir=self.root, status="finished",
                    exit_code=0)
        with self.assertRaises(ValueError):
            status(state_dir=self.root)
        def must_not_send(*args, **kwargs):
            self.fail("untrusted outbox entry was sent to GitHub")
        self.assertEqual(flush(state_dir=self.root, runner=must_not_send),
                         {"sent": 0, "pending": 1})
        self.assertFalse(list((self.root / "delivered").glob("*.json")))

    def test_corrupt_delivered_marker_does_not_reopen_slot(self):
        from gway_agent_ops.admission_status import status
        from gway_agent_ops.observation import observe
        from gway_agent_ops.reporting import _key
        self.root.mkdir(mode=0o700)
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": self.issue, "at": self.at}) + "\n")
        delivered = self.root / "delivered"
        delivered.mkdir()
        path = delivered / (_key(self.issue, self.at) + ".json")
        path.write_text(json.dumps({"status": "finished", "exit_code": 0,
                                    "marker": "agent-dispatch:other"}))
        with self.assertRaisesRegex(ValueError, "invalid report journal"):
            observe(self.issue, self.at, state_dir=self.root)
        with self.assertRaises(ValueError):
            status(state_dir=self.root)

    def test_other_issue_same_timestamp_is_distinct(self):
        a = enqueue(self.issue, self.at, state_dir=self.root)
        b = enqueue("arthexis/other#20", self.at, state_dir=self.root)
        self.assertNotEqual(a["marker"], b["marker"])
