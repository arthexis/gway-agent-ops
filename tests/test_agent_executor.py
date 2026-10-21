"""A3.3 trusted one-shot Codex invocation; subprocess mocked."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.executor import execute, ExecutionError
from gway_agent_ops.dispatch import Duplicate


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
        def runner(argv, **options):
            seen.append((argv, options))
            return subprocess.CompletedProcess(argv, 0, "TOKEN-NEVER-PRINT", "")
        def resolver(ref, **options):
            self.assertEqual(options["allowed_repositories"], ("arthexis/example",))
            return self.issue
        def runtime():
            home = self.root / "codex-home"
            home.mkdir(exist_ok=True)
            (home / "config.toml").write_text('model = "test"')
            return {"codex_home": str(home), "authentication_available": True,
                    "environment": {"CODEX_HOME": str(home), "PATH": "/usr/bin"}}
        result = execute("arthexis/example#19", at="2026-10-13T01:00:00Z",
                         allowed_repositories=("arthexis/example",),
                         workspace_root=self.root, state_dir=self.state,
                         resolver=resolver, runner=runner, runtime=runtime, **kwargs)
        return result, seen

    def test_exec_args_lock_and_metadata(self):
        result, seen = self.call()
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["comment_baseline"], 6)
        self.assertTrue(result["issue_revision"].startswith("sha256:"))
        argv, options = seen[0]
        self.assertEqual(argv, ["codex", "exec", "--sandbox", "workspace-write", "-"])
        self.assertEqual(options["cwd"], self.checkout)
        self.assertIn("Body", options["input"])
        self.assertNotIn("TOKEN-NEVER-PRINT", str(result))
        with self.assertRaises(Duplicate):
            self.call()

    def test_no_checkout_prevents_dispatch(self):
        (self.checkout / ".git").rmdir()
        with self.assertRaisesRegex(ExecutionError, "workspace_unavailable"):
            self.call()
        self.assertFalse(self.state.exists())

    def test_timeout_validation(self):
        with self.assertRaises(ValueError):
            self.call(timeout_seconds=0)
        self.assertFalse(self.state.exists())

    def test_issue_resolution_failure_prevents_dispatch(self):
        def blocked(*args, **kwargs):
            raise ValueError("not authorized")
        with self.assertRaises(ValueError):
            execute("arthexis/example#19", allowed_repositories=("arthexis/example",),
                    workspace_root=self.root, resolver=blocked)
        self.assertFalse(self.state.exists())
