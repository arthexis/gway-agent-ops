import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.admission_status import status
from gway_agent_ops.nonblocking import submit, release


class AdmissionStatusTests(unittest.TestCase):
    def test_idle_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(status(state_dir=root), {
                "state": "idle", "available": True, "active_issue": None, "active_at": None})
            self.assertEqual(list(root.iterdir()), [])

    def test_reserved_and_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
                submit("owner/repo#1", "2026-10-13T15:00:00Z",
                       allowed_repositories=("owner/repo",), state_dir=root)
                observed = status(state_dir=root)
                self.assertEqual(observed["state"], "reserved")
                self.assertFalse(observed["available"])
                self.assertEqual(observed["active_issue"], "owner/repo#1")

    def test_running_lock_blocks_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fd = os.open(root / "execution.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(status(state_dir=root)["state"], "running")
                self.assertFalse(status(state_dir=root)["available"])
            finally:
                os.close(fd)

    def test_unresolved_direct_acceptance_blocks_remote_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref, at = "owner/repo#1", "2026-10-13T15:00:00.000000Z"
            (root / "accepted.jsonl").write_text(
                json.dumps({"issue": ref, "at": at}) + "\\n")
            snapshot = status(state_dir=root)
            self.assertFalse(snapshot["available"])
            self.assertEqual(snapshot["active_issue"], ref)
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}):
                response = submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                  allowed_repositories=("owner/repo",), state_dir=root)
            self.assertEqual(response["state"], "busy")

    def test_corrupt_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requests").mkdir()
            (root / "requests" / "bad.json").write_text(json.dumps({"issue": 1}))
            with self.assertRaises(ValueError):
                status(state_dir=root)


if __name__ == "__main__":
    unittest.main()
