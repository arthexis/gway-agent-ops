"""Failure and concurrency boundaries of the single-slot controller."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.dispatch import admission, Busy, timestamp
from gway_agent_ops.admission_status import status
from gway_agent_ops.nonblocking import submit, release
from gway_agent_ops.reporting import _key


class ControllerHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_direct_accepted_without_terminal_blocks_restart_and_submit(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with admission(ref, at, state_dir=self.root):
            pass
        self.assertFalse(status(state_dir=self.root)["available"])
        with self.assertRaises(Busy):
            with admission("owner/repo#2", "2026-10-13T15:01:00Z",
                           state_dir=self.root):
                pass
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}):
            response = submit("owner/repo#2", "2026-10-13T15:01:00Z",
                              allowed_repositories=("owner/repo",),
                              state_dir=self.root)
        self.assertEqual(response["state"], "busy")

    def test_release_cannot_run_during_execution(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
             patch("gway_agent_ops.nonblocking.subprocess.run", return_value=Mock(returncode=1)):
            submit(ref, at, allowed_repositories=("owner/repo",),
                   state_dir=self.root)
        lock = self.root / "execution.lock"
        fd = os.open(lock, os.O_RDWR)
        runner = Mock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "execution_active"):
                release(ref, at, state_dir=self.root, runner=runner)
            runner.assert_not_called()
        finally:
            os.close(fd)

    def test_ambiguous_launch_persists_and_same_key_does_not_retry(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        runner = Mock(side_effect=OSError("systemd launch uncertain"))
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
             patch("gway_agent_ops.nonblocking.subprocess.run", return_value=Mock(returncode=1)):
            first = submit(ref, at, allowed_repositories=("owner/repo",),
                           state_dir=self.root, runner=runner)
            again = submit(ref, at, allowed_repositories=("owner/repo",),
                           state_dir=self.root, runner=runner)
        self.assertEqual(first["state"], "launch_unconfirmed")
        self.assertEqual(again["state"], "already_requested")
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(status(state_dir=self.root)["available"])

    def test_status_during_submission_gate_is_unavailable(self):
        requests = self.root / "requests"
        requests.mkdir()
        fd = os.open(requests / "submission.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            snapshot = status(state_dir=self.root)
            self.assertFalse(snapshot["available"])
            self.assertEqual(snapshot["state"], "uncertain")
        finally:
            os.close(fd)

    def test_competing_processes_cannot_both_admit(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with admission(ref, at, state_dir=self.root):
            program = "\n".join([
                "from pathlib import Path",
                "from gway_agent_ops.dispatch import admission, Busy",
                "try:",
                f" with admission('owner/repo#2', '2026-10-13T15:01:00Z', state_dir=Path({str(self.root)!r})): pass",
                "except Busy: raise SystemExit(42)",
                "",
            ])
            child = subprocess.run([sys.executable, "-c", program],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 42, child.stderr)
        # A released OS lock is insufficient without durable terminal evidence.
        with self.assertRaises(Busy):
            with admission("owner/repo#2", "2026-10-13T15:01:00Z",
                           state_dir=self.root):
                pass

    def test_crashed_worker_does_not_reopen_slot(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        program = "\n".join([
            "from pathlib import Path",
            "from gway_agent_ops.dispatch import admission",
            f"with admission({ref!r}, {at!r}, state_dir=Path({str(self.root)!r})): pass",
            "",
        ])
        child = subprocess.run([sys.executable, "-c", program],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(status(state_dir=self.root)["available"])
        with self.assertRaises(Busy):
            with admission("owner/repo#2", "2026-10-13T15:01:00Z",
                           state_dir=self.root):
                pass

    def test_release_is_idempotent_and_allows_new_issue_only(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
             patch("gway_agent_ops.nonblocking.subprocess.run", return_value=Mock(returncode=1)):
            submit(ref, at, allowed_repositories=("owner/repo",),
                   state_dir=self.root)
            runner = Mock(side_effect=[
                Mock(returncode=0, stdout=""),
                Mock(returncode=3, stdout="inactive")])
            self.assertEqual(release(ref, at, state_dir=self.root, runner=runner)["state"], "released")
            self.assertEqual(release(ref, at, state_dir=self.root, runner=runner)["state"], "already_terminal")
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(submit(ref, at, allowed_repositories=("owner/repo",),
                                    state_dir=self.root)["state"], "already_requested")
            self.assertEqual(submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                    allowed_repositories=("owner/repo",),
                                    state_dir=self.root)["state"], "launch_unconfirmed")

    def test_completion_evidence_unlocks_next_admission(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with admission(ref, at, state_dir=self.root):
            pass
        folder = self.root / "delivered"
        folder.mkdir()
        path = folder / (_key(ref, timestamp(at)) + ".json")
        path.write_text(json.dumps({"status": "finished", "exit_code": 0}))
        self.assertTrue(status(state_dir=self.root)["available"])
        with admission("owner/repo#2", "2026-10-13T15:01:00Z",
                       state_dir=self.root):
            self.assertFalse(status(state_dir=self.root)["available"])

    def test_collected_transient_unit_may_be_released(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"):
            submit(ref, at, allowed_repositories=("owner/repo",),
                   state_dir=self.root, runner=Mock(return_value=Mock(returncode=1)))
        runner = Mock(side_effect=[
            Mock(returncode=5, stdout=""),
            Mock(returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n"),
            Mock(returncode=3, stdout="unknown\n")])
        result = release(ref, at, state_dir=self.root, runner=runner)
        self.assertEqual(result["state"], "released")
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(status(state_dir=self.root)["available"], True)

    def test_collected_unit_rejects_uncertain_or_active_systemd_state(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"):
            submit(ref, at, allowed_repositories=("owner/repo",),
                   state_dir=self.root, runner=Mock(return_value=Mock(returncode=1)))
        for properties in ("LoadState=loaded\nActiveState=active\n",
                           "LoadState=not-found\nActiveState=activating\n",
                           "LoadState=not-found\n",
                           "LoadState=not-found\nActiveState=inactive\n"):
            with self.subTest(properties=properties):
                code = 1 if properties.endswith("inactive\n") else 0
                runner = Mock(side_effect=[
                    Mock(returncode=5, stdout=""),
                    Mock(returncode=code, stdout=properties)])
                with self.assertRaisesRegex(ValueError, "unit_stop_unconfirmed"):
                    release(ref, at, state_dir=self.root, runner=runner)
                self.assertFalse(status(state_dir=self.root)["available"])

    def test_failed_release_keeps_reservation_after_restart(self):
        ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
             patch("gway_agent_ops.nonblocking.subprocess.run", return_value=Mock(returncode=1)):
            submit(ref, at, allowed_repositories=("owner/repo",),
                   state_dir=self.root)
        runner = Mock(return_value=Mock(returncode=1, stdout=""))
        with self.assertRaisesRegex(ValueError, "unit_stop_unconfirmed"):
            release(ref, at, state_dir=self.root, runner=runner)
        self.assertFalse(status(state_dir=self.root)["available"])
        self.assertEqual(status(state_dir=self.root)["active_issue"], ref)


if __name__ == "__main__":
    unittest.main()
