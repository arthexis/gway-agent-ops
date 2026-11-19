"""Stale reservation recovery requires actual, inactive systemd evidence."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.capacity import unresolved
from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe
from gway_agent_ops.stale_recovery import PENDING_GRACE, reconcile_pending

REF = "owner/repo#1"
AT = "2026-10-13T15:00:00Z"
NOW = datetime(2026, 10, 13, 15, 6, tzinfo=timezone.utc)
ABSENT = ("LoadState=not-found\nActiveState=inactive\nJob=0\n"
          "MainPID=0\nControlPID=0\nTasksCurrent=0\nControlGroup=\n")
ACTIVE = ("LoadState=loaded\nActiveState=active\nJob=0\n"
          "MainPID=123\nControlPID=0\nTasksCurrent=1\n"
          "ControlGroup=/user.slice/test.service\n")


class StaleRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bus = patch("gway_agent_ops.stale_recovery.systemd_user_environment",
                         return_value={"XDG_RUNTIME_DIR": "/run/user/1000"})
        self.bus.start()
        self.addCleanup(self.bus.stop)
        self.release_bus = patch("gway_agent_ops.nonblocking.systemd_user_environment",
                                 return_value={"XDG_RUNTIME_DIR": "/run/user/1000"})
        self.release_bus.start()
        self.addCleanup(self.release_bus.stop)

    def seed(self):
        with patch("gway_agent_ops.nonblocking.resolve", return_value={"labels": []}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/fake/agent"):
            out = submit(REF, AT, state_dir=self.root,
                         runner=Mock(return_value=Mock(returncode=0)))
        self.assertEqual(out["state"], "requested")

    @staticmethod
    def runner(unit_state):
        calls = []
        def run(argv, **_):
            calls.append(argv)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return Mock(returncode=0, stdout=unit_state)
            if argv[:3] == ["systemctl", "--user", "stop"]:
                return Mock(returncode=0, stdout="")
            if argv[:3] == ["systemctl", "--user", "is-active"]:
                return Mock(returncode=3, stdout="inactive\n")
            raise AssertionError(argv)
        return run, calls

    def test_absent_old_unit_released_once_and_same_identity_never_reused(self):
        self.seed()
        run, calls = self.runner(ABSENT)
        outcome = reconcile_pending(state_dir=self.root, now=NOW, runner=run)
        self.assertEqual(outcome["recovered"], [REF])
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "released")
        self.assertEqual(len([x for x in calls if x[2] == "stop"]), 1)
        self.assertEqual(reconcile_pending(state_dir=self.root, now=NOW, runner=run)
                         ["recovered"], [])
        self.assertEqual(unresolved(self.root), [])

    def test_running_or_ambiguous_workers_never_released(self):
        for state in (ACTIVE, "LoadState=not-found\nActiveState=inactive\n",
                      ABSENT.replace("Job=0", "Job=9")):
            with self.subTest(state=state):
                self.seed()
                run, calls = self.runner(state)
                self.assertEqual(reconcile_pending(state_dir=self.root,
                                 now=NOW, runner=run)["recovered"], [])
                self.assertEqual(observe(REF, AT, state_dir=self.root)["state"],
                                 "requested")
                self.assertFalse(any(argv[2] == "stop" for argv in calls))
                # Reset the synthetic state, keeping all files local to test.
                self.root.joinpath("requests").joinpath(next(
                    p.name for p in (self.root / "requests").glob("*.json"))).unlink()

    def test_grace_period_and_accepted_worker_preserved(self):
        self.seed()
        run, calls = self.runner(ABSENT)
        early = datetime(2026, 10, 13, 15, 4, tzinfo=timezone.utc)
        self.assertEqual(reconcile_pending(state_dir=self.root, now=early,
                                          runner=run)["recovered"], [])
        self.assertEqual(calls, [])
        (self.root / "accepted.jsonl").write_text(json.dumps({
            "issue": REF, "at": "2026-10-13T15:00:00.000000Z"}) + "\n")
        self.assertEqual(reconcile_pending(state_dir=self.root, now=NOW,
                                          runner=run)["recovered"], [])
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
        self.assertEqual(calls, [])

    def test_unavailable_systemd_never_turns_uncertainty_into_terminal(self):
        self.seed()
        with patch("gway_agent_ops.stale_recovery.systemd_user_environment",
                   side_effect=ValueError("bus unavailable")):
            result = reconcile_pending(state_dir=self.root, now=NOW,
                                       runner=Mock())
        self.assertEqual(result["unresolved"], [REF])
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"],
                         "requested")


if __name__ == "__main__":
    unittest.main()
