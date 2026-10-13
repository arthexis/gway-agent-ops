"""Failure and concurrency boundaries of the single-slot controller."""
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.dispatch import admission, Busy
from gway_agent_ops.admission_status import status
from gway_agent_ops.nonblocking import submit, release


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
             patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
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
        runner = Mock()
        with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
             patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
            first = submit(ref, at, allowed_repositories=("owner/repo",),
                           state_dir=self.root, runner=runner)
            again = submit(ref, at, allowed_repositories=("owner/repo",),
                           state_dir=self.root, runner=runner)
        self.assertEqual(first["state"], "launch_unconfirmed")
        self.assertEqual(again["state"], "already_requested")
        runner.assert_not_called()
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


if __name__ == "__main__":
    unittest.main()
