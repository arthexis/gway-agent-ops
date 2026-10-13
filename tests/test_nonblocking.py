import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gway_agent_ops.nonblocking import submit, release
from gway_agent_ops.observation import observe


class NonblockingTests(unittest.TestCase):
    def test_once_only_and_observable(self):
        ref, at = "arthexis/example#3", "2026-10-13T15:00:00Z"
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"GWAY_AGENT_WORKSPACE_ROOT": "/tmp/workspace"}), \
                 patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value="/usr/bin/agent"), \
                 patch("gway_agent_ops.nonblocking.Path.is_file", return_value=True):
                runner = Mock(return_value=Mock(returncode=0))
                first = submit(ref, at, allowed_repositories=("arthexis/example",),
                               state_dir=Path(temp), runner=runner)
                self.assertEqual(first["state"], "requested")
                self.assertEqual(observe(ref, at, state_dir=Path(temp))["state"], "requested")
                second = submit(ref, at, allowed_repositories=("arthexis/example",),
                                state_dir=Path(temp), runner=runner)
                self.assertEqual(second["state"], "already_requested")
                self.assertEqual(runner.call_count, 1)
                self.assertEqual(runner.call_args.args[0][-6:],
                                 ["/usr/bin/agent", "issue", ref, "--at",
                                  "2026-10-13T15:00:00.000000Z", "--json"])

    def test_trust_rejection_never_launches(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("gway_agent_ops.nonblocking.resolve", side_effect=ValueError("rejected")):
                runner = Mock()
                with self.assertRaises(ValueError):
                    submit("owner/repo#1", "2026-10-13T15:00:00Z",
                           allowed_repositories=("owner/repo",),
                           state_dir=Path(temp), runner=runner)
                runner.assert_not_called()

    def test_different_issue_blocked_while_request_unresolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
                first = submit("owner/repo#1", "2026-10-13T15:00:00Z",
                               allowed_repositories=("owner/repo",), state_dir=root)
                self.assertEqual(first["state"], "launch_unconfirmed")
                second = submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                allowed_repositories=("owner/repo",), state_dir=root)
                self.assertEqual(second["state"], "busy")
                self.assertEqual(second["active_issue"], "owner/repo#1")
                self.assertEqual(len(list((root / "requests").glob("*.json"))), 1)
                again = submit("owner/repo#1", "2026-10-13T15:00:00Z",
                               allowed_repositories=("owner/repo",), state_dir=root)
                self.assertEqual(again["state"], "already_requested")

    def test_terminal_request_allows_next_issue(self):
        import json
        from gway_agent_ops.reporting import _key
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref, at = "owner/repo#1", "2026-10-13T15:00:00.000000Z"
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
                submit(ref, at, allowed_repositories=("owner/repo",), state_dir=root)
                (root / "accepted.jsonl").write_text(json.dumps({"issue": ref, "at": at}) + "\n")
                (root / "delivered").mkdir()
                (root / "delivered" / (_key(ref, at) + ".json")).write_text(
                    json.dumps({"status": "finished", "exit_code": 0}))
                second = submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                allowed_repositories=("owner/repo",), state_dir=root)
                self.assertEqual(second["state"], "launch_unconfirmed")

    def test_release_requires_confirmed_stopped_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
                submit(ref, at, allowed_repositories=("owner/repo",), state_dir=root)
                runner = Mock(return_value=Mock(returncode=1, stdout=""))
                with self.assertRaisesRegex(ValueError, "unit_stop_unconfirmed"):
                    release(ref, at, state_dir=root, runner=runner)
                self.assertEqual(observe(ref, at, state_dir=root)["state"], "requested")

    def test_release_is_durable_and_does_not_retry_original_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref, at = "owner/repo#1", "2026-10-13T15:00:00Z"
            with patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value=None):
                submit(ref, at, allowed_repositories=("owner/repo",), state_dir=root)
                runner = Mock(side_effect=[Mock(returncode=0, stdout=""),
                                           Mock(returncode=3, stdout="inactive\\n")])
                self.assertEqual(release(ref, at, state_dir=root, runner=runner)["state"], "released")
                self.assertEqual(observe(ref, at, state_dir=root)["state"], "released")
                self.assertEqual(submit(ref, at, allowed_repositories=("owner/repo",),
                                        state_dir=root)["state"], "already_requested")
                self.assertEqual(submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                        allowed_repositories=("owner/repo",),
                                        state_dir=root)["state"], "launch_unconfirmed")

    def test_direct_execution_lock_blocks_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "execution.lock"
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("gway_agent_ops.nonblocking.resolve", return_value={}):
                    result = submit("owner/repo#2", "2026-10-13T15:01:00Z",
                                    allowed_repositories=("owner/repo",), state_dir=root)
                self.assertEqual(result["state"], "busy")
                self.assertFalse(list((root / "requests").glob("*.json")))
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
