"""Single-attempt submission and durable release semantics."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.nonblocking import submit, release, _launch_prerequisites
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import _key

REF = "owner/repo#1"
AT = "2026-10-13T15:00:00Z"
REPOS = ("owner/repo",)


class NonblockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gate = patch("gway_agent_ops.nonblocking.resolve", return_value={})
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def send(self, ref=REF, at=AT, *, exit_code=0):
        with patch("gway_agent_ops.nonblocking._launch_prerequisites",
                   return_value="/usr/bin/agent"):
            return submit(ref, at, allowed_repositories=REPOS, state_dir=self.root,
                          runner=Mock(return_value=Mock(returncode=exit_code)))

    def test_once_only_and_observable(self):
        self.assertEqual(self.send()["state"], "requested")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "requested")
        self.assertEqual(self.send()["state"], "already_requested")
        self.assertEqual(len(list((self.root / "requests").glob("*.json"))), 1)

    def test_preflight_failure_does_not_write_request(self):
        for failure in ("agent_workspace_not_configured", "agent_cli_not_found",
                        "agent_workspace_unsafe", "agent_user_bus_unavailable"):
            with self.subTest(failure=failure), patch(
                    "gway_agent_ops.nonblocking._launch_prerequisites",
                    side_effect=ValueError(failure)):
                with self.assertRaisesRegex(ValueError, failure):
                    submit(REF, AT, allowed_repositories=REPOS, state_dir=self.root,
                           runner=Mock())
                self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "unknown")
                self.assertFalse(list((self.root / "requests").glob("*.json")))

    def test_launch_failure_keeps_marker_and_blocks_other_issue(self):
        self.assertEqual(self.send(exit_code=1)["state"], "launch_unconfirmed")
        self.assertEqual(self.send("owner/repo#2")["state"], "busy")
        self.assertEqual(self.send()["state"], "already_requested")

    def test_trust_rejection_does_not_launch_or_write(self):
        with patch("gway_agent_ops.nonblocking.resolve", side_effect=ValueError("rejected")):
            with self.assertRaisesRegex(ValueError, "rejected"):
                self.send()
        self.assertFalse(list((self.root / "requests").glob("*.json")))

    def test_terminal_request_allows_next_issue(self):
        self.send()
        normalized = "2026-10-13T15:00:00.000000Z"
        (self.root / "accepted.jsonl").write_text(json.dumps({
            "issue": REF, "at": normalized}) + "\n")
        (self.root / "delivered").mkdir()
        (self.root / "delivered" / (_key(REF, normalized) + ".json")).write_text(
            json.dumps({"status": "finished", "exit_code": 0}))
        self.assertEqual(self.send("owner/repo#2")["state"], "requested")

    def test_release_requires_inactive_unit_and_preserves_identity(self):
        self.send(exit_code=1)
        failed = Mock(return_value=Mock(returncode=1, stdout=""))
        with self.assertRaisesRegex(ValueError, "unit_stop_unconfirmed"):
            release(REF, AT, state_dir=self.root, runner=failed)
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "requested")
        good = Mock(side_effect=[Mock(returncode=0, stdout=""),
                                 Mock(returncode=3, stdout="inactive\n")])
        self.assertEqual(release(REF, AT, state_dir=self.root, runner=good)["state"], "released")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "released")
        self.assertEqual(self.send()["state"], "already_requested")
        self.assertEqual(self.send("owner/repo#2")["state"], "requested")

    def test_real_prerequisite_validates_workspace_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            os.chmod(workspace, 0o700)
            env = {"GWAY_AGENT_WORKSPACE_ROOT": str(workspace), "XDG_RUNTIME_DIR": "/run/user/1000"}
            with patch.dict(os.environ, env), patch("gway_agent_ops.nonblocking.shutil.which",
                    side_effect=lambda name: "/bin/true"), patch(
                    "gway_agent_ops.nonblocking.Path.is_file", return_value=True):
                self.assertEqual(_launch_prerequisites(), "/bin/true")
                os.chmod(workspace, 0o755)
                with self.assertRaisesRegex(ValueError, "agent_workspace_unsafe"):
                    _launch_prerequisites()
            os.chmod(workspace, 0o700)

    def test_missing_workspace_is_specific(self):
        with patch.dict(os.environ, {"GWAY_AGENT_WORKSPACE_ROOT": ""}), patch(
                "gway_agent_ops.nonblocking.shutil.which", return_value="/bin/true"), patch(
                "gway_agent_ops.nonblocking.Path.is_file", return_value=True):
            with self.assertRaisesRegex(ValueError, "agent_workspace_not_configured"):
                _launch_prerequisites()


if __name__ == "__main__":
    unittest.main()
