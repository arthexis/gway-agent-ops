"""A3.3 trusted one-shot Codex invocation; subprocess mocked."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.executor import execute, ExecutionError
from gway_agent_ops.dispatch import Duplicate, Busy, admission, timestamp
from gway_agent_ops.observation import observe
from gway_agent_ops.admission_status import status


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "arthexis" / "example"
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").mkdir()
        self.state = self.root / "state"
        self.issue = {"repository": "arthexis/example", "number": 19,
                      "title": "Fix an issue", "body": "Body", "comments": 6, "labels": ["gway-001"],
                      "url": "https://github.com/arthexis/example/issues/19"}

    def call(self, **kwargs):
        seen = kwargs.pop("seen", [])
        override = kwargs.pop("runner_override", None)
        def runner(argv, **options):
            seen.append((argv, options))
            return subprocess.CompletedProcess(argv, 0, "TOKEN-NEVER-PRINT", "")
        def resolver(ref, **options):
            self.assertEqual(options["allowed_repositories"], ("arthexis/example",))
            return self.issue
        def runtime():
            home = self.root / "codex-home"
            home.mkdir(exist_ok=True)
            (home / "config.toml").write_text(
                'model = "test"\n'
                'sandbox_mode = "danger-full-access"\n'
                'approval_policy = "never"\n')
            return {"codex_home": str(home), "authentication_available": True,
                    "environment": {"CODEX_HOME": str(home), "PATH": "/usr/bin"}}
        result = execute("arthexis/example#19", at="2026-10-13T01:00:00Z",
                         allowed_repositories=("arthexis/example",),
                         workspace_root=self.root, state_dir=self.state,
                         resolver=resolver, runner=override or runner,
                         runtime=runtime, **kwargs)
        return result, seen

    def test_normal_zero_is_codex_finished_not_host_complete(self):
        result, seen = self.call()
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["admin_work_state"], "not_attested")
        self.assertEqual(result["comment_baseline"], 6)
        self.assertTrue(result["issue_revision"].startswith("sha256:"))
        argv, options = seen[0]
        self.assertEqual(argv, ["codex", "exec", "-"])
        self.assertEqual(options["cwd"], self.checkout)
        self.assertIn("Body", options["input"])
        self.assertNotIn("TOKEN-NEVER-PRINT", str(result))
        observed = observe("arthexis/example#19", "2026-10-13T01:00:00Z",
                           state_dir=self.state)
        self.assertEqual(observed["state"], "finished")
        self.assertTrue(observed["execution_terminal"])
        self.assertEqual(observed["admin_work_state"], "not_attested")
        self.assertTrue(status(state_dir=self.state)["available"])
        with self.assertRaises(Duplicate):
            self.call()
        with admission("arthexis/example#27", "2026-10-13T01:00:00Z",
                       state_dir=self.state):
            pass

    def test_adopted_codex_config_remains_authoritative_and_unchanged(self):
        seen = []
        result, _ = self.call(seen=seen)
        argv, options = seen[0]
        self.assertEqual(argv, ["codex", "exec", "-"])
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--ask-for-approval", argv)
        self.assertNotIn("--config", argv)
        self.assertEqual(options["env"]["CODEX_HOME"],
                         str(self.root / "codex-home"))
        self.assertEqual((self.root / "codex-home" / "config.toml").read_text(),
                         'model = "test"\n'
                         'sandbox_mode = "danger-full-access"\n'
                         'approval_policy = "never"\n')
        self.assertEqual(result["status"], "finished")

    def test_normal_nonzero_is_codex_failed_not_host_complete(self):
        seen = []
        def failed_runner(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 37, "", "synthetic")
        result, _ = self.call(runner_override=failed_runner)
        self.assertEqual(result["status"], "codex_failed")
        self.assertEqual(result["exit_code"], 37)
        self.assertEqual(result["admin_work_state"], "not_attested")
        self.assertEqual(len(seen), 1)
        observed = observe("arthexis/example#19", "2026-10-13T01:00:00Z",
                           state_dir=self.state)
        self.assertEqual(observed["state"], "codex_failed")
        self.assertEqual(observed["admin_work_state"], "not_attested")
        self.assertTrue(status(state_dir=self.state)["available"])
        with self.assertRaises(Duplicate):
            self.call(runner_override=failed_runner)
        self.assertEqual(len(seen), 1)

    def test_terminal_outbox_failure_preserves_accepted_reservation(self):
        from unittest.mock import patch
        with patch("gway_agent_ops.executor.enqueue",
                   side_effect=OSError("synthetic journal failure")):
            with self.assertRaisesRegex(OSError, "synthetic journal failure"):
                self.call()
        self.assertEqual(observe("arthexis/example#19", "2026-10-13T01:00:00Z",
                                 state_dir=self.state)["state"], "accepted")
        self.assertFalse(status(state_dir=self.state)["available"])
        self.assertEqual(list((self.state / "outbox").glob("*.json")), [])
        with self.assertRaises(Duplicate):
            self.call()

    def test_timed_out_codex_cannot_fabricate_terminal_or_free_slot(self):
        def timeout_runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 1)
        with self.assertRaisesRegex(ExecutionError, "codex_timeout"):
            self.call(runner_override=timeout_runner)
        key, at = "arthexis/example#19", "2026-10-13T01:00:00Z"
        self.assertEqual(observe(key, at, state_dir=self.state)["state"], "accepted")
        self.assertFalse(status(state_dir=self.state)["available"])
        self.assertEqual(list((self.state / "outbox").glob("*.json")), [])
        self.assertEqual(list((self.state / "delivered").glob("*.json")), [])
        with self.assertRaises(Busy):
            with admission("arthexis/example#27", timestamp(at),
                           state_dir=self.state):
                pass

    def test_uncertain_spawn_error_preserves_accepted_identity(self):
        def broken_runner(args, **kwargs):
            raise OSError("synthetic exec failure")
        with self.assertRaisesRegex(ExecutionError, "codex_unavailable"):
            self.call(runner_override=broken_runner)
        self.assertEqual(observe("arthexis/example#19", "2026-10-13T01:00:00Z",
                                 state_dir=self.state)["state"], "accepted")
        self.assertEqual(list((self.state / "outbox").glob("*.json")), [])
        self.assertFalse(status(state_dir=self.state)["available"])

    def test_no_checkout_prevents_dispatch(self):
        (self.checkout / ".git").rmdir()
        with self.assertRaisesRegex(ExecutionError, "workspace_unavailable"):
            self.call()
        self.assertFalse(self.state.exists())

    def test_timeout_validation(self):
        with self.assertRaises(ValueError):
            self.call(timeout_seconds=0)
        self.assertFalse(self.state.exists())

    def test_label_removed_before_execution_never_starts_codex(self):
        from gway_agent_ops.github_issues import IssueRejected
        self.issue["labels"] = []
        with self.assertRaisesRegex(IssueRejected, "target_label_missing"):
            self.call()
        self.assertFalse(self.state.exists())

    def test_issue_resolution_failure_prevents_dispatch(self):
        def blocked(*args, **kwargs):
            raise ValueError("not authorized")
        with self.assertRaises(ValueError):
            execute("arthexis/example#19", allowed_repositories=("arthexis/example",),
                    workspace_root=self.root, resolver=blocked)
        self.assertFalse(self.state.exists())
