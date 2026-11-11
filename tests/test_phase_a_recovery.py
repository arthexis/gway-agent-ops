"""Offline Phase A: uncertain starts and operator-confirmed recovery."""
import fcntl
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.admission_status import status
from gway_agent_ops.dispatch import timestamp
from gway_agent_ops.nonblocking import release, submit
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import enqueue, flush

REF = "arthexis/example#19"
AT = "2026-10-13T15:00:00Z"
REPOS = ("arthexis/example",)


class PhaseARecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "dispatch"
        self.root.mkdir(mode=0o700)
        trusted = patch("gway_agent_ops.nonblocking.resolve",
                        return_value={"labels": ["gway-001"]})
        trusted.start()
        self.addCleanup(trusted.stop)

    def submit(self, *, ref=REF, runner=None):
        with patch("gway_agent_ops.nonblocking._launch_prerequisites",
                   return_value="/usr/bin/agent"):
            return submit(ref, AT, allowed_repositories=REPOS, state_dir=self.root,
                          runner=runner or Mock(return_value=Mock(returncode=0)))

    @staticmethod
    def stopped_unit():
        return Mock(side_effect=[
            Mock(returncode=0, stdout=""),
            Mock(returncode=3, stdout="inactive\n"),
        ])

    @staticmethod
    def github_ok(argv, **kwargs):
        if "per_page=100" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def test_launch_uncertainty_is_durable_and_cannot_retry(self):
        self.assertEqual(self.submit(runner=Mock(return_value=Mock(returncode=1)))
                         ["state"], "launch_unconfirmed")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"],
                         "launch_unconfirmed")
        self.assertEqual(status(state_dir=self.root)["state"], "uncertain")
        self.assertFalse(status(state_dir=self.root)["available"])
        self.assertEqual(self.submit()["state"], "already_requested")
        self.assertEqual(self.submit(ref="arthexis/example#20")["state"], "busy")
        self.assertEqual(len(list((self.root / "requests").glob("*.json"))), 1)

    def test_timeout_is_uncertain_and_not_reexecuted(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired(["systemd-run"], 5))
        self.assertEqual(self.submit(runner=runner)["state"], "launch_unconfirmed")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"],
                         "launch_unconfirmed")
        self.assertEqual(self.submit()["state"], "already_requested")
        self.assertEqual(runner.call_count, 1)

    def test_confirmed_stop_of_accepted_orphan_leaves_report(self):
        self.submit()
        normalized = timestamp(AT)
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": REF, "at": normalized}) + "\n")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
        self.assertEqual(release(REF, AT, state_dir=self.root, runner=self.stopped_unit())
                         ["state"], "released")
        found = list((self.root / "outbox").glob("*.json"))
        self.assertEqual(len(found), 1)
        self.assertEqual(json.loads(found[0].read_text())["status"], "interrupted")
        observed = observe(REF, AT, state_dir=self.root)
        self.assertEqual(observed["state"], "released")
        self.assertTrue(observed["execution_terminal"])
        self.assertFalse(observed["delivery_confirmed"])
        self.assertEqual(self.submit()["state"], "already_requested")
        self.assertEqual(self.submit(ref="arthexis/example#20")["state"], "requested")
        self.assertEqual(flush(state_dir=self.root, runner=self.github_ok),
                         {"sent": 1, "pending": 0})
        self.assertTrue(observe(REF, AT, state_dir=self.root)["delivery_confirmed"])
        self.assertEqual(flush(state_dir=self.root, runner=self.github_ok),
                         {"sent": 0, "pending": 0})

    def test_ambiguous_stop_neither_releases_nor_reports(self):
        self.submit(runner=Mock(return_value=Mock(returncode=1)))
        stop_failed = Mock(return_value=Mock(returncode=1, stdout=""))
        with self.assertRaisesRegex(ValueError, "unit_stop_unconfirmed"):
            release(REF, AT, state_dir=self.root, runner=stop_failed)
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"],
                         "launch_unconfirmed")
        self.assertFalse((self.root / "outbox").exists())
        self.assertFalse((self.root / "released").exists())

    def test_unaccepted_launch_release_does_not_claim_execution(self):
        self.submit(runner=Mock(return_value=Mock(returncode=1)))
        self.assertEqual(release(REF, AT, state_dir=self.root, runner=self.stopped_unit())
                         ["state"], "released")
        self.assertFalse((self.root / "outbox").exists())
        self.assertTrue(observe(REF, AT, state_dir=self.root)["execution_terminal"])

    def test_active_execution_lock_prevents_release(self):
        self.submit()
        descriptor = os.open(self.root / "execution.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "execution_active"):
                release(REF, AT, state_dir=self.root, runner=Mock())
            self.assertFalse((self.root / "released").exists())
        finally:
            os.close(descriptor)

    def test_findings_validation_visible_before_and_after_delivery(self):
        self.submit()
        normalized = timestamp(AT)
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": REF, "at": normalized}) + "\n")
        enqueue(REF, normalized, state_dir=self.root, status="finished",
                exit_code=0, findings_state="missing_or_invalid")
        pending = observe(REF, AT, state_dir=self.root)
        self.assertEqual(pending["state"], "finished")
        self.assertEqual(pending["findings_state"], "missing_or_invalid")
        self.assertFalse(pending["delivery_confirmed"])
        self.assertEqual(flush(state_dir=self.root, runner=self.github_ok),
                         {"sent": 1, "pending": 0})
        done = observe(REF, AT, state_dir=self.root)
        self.assertEqual(done["findings_state"], "missing_or_invalid")
        self.assertTrue(done["delivery_confirmed"])

    def test_direct_orphan_not_auto_recovered_or_reexecuted(self):
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": REF, "at": timestamp(AT)}) + "\n")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
        self.assertEqual(status(state_dir=self.root)["state"], "uncertain")
        self.assertEqual(self.submit(ref="arthexis/example#20")["state"], "busy")
        self.assertFalse((self.root / "outbox").exists())


if __name__ == "__main__":
    unittest.main()
