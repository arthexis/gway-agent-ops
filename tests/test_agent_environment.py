import os
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.environment import load_runtime_environment


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runtime.env"

    def write(self, text, mode=0o600):
        self.path.write_text(text)
        self.path.chmod(mode)

    def test_missing_file_preserves_fail_closed_environment(self):
        environment = {}
        self.assertEqual(load_runtime_environment(path=self.path, environ=environment), ())
        self.assertEqual(environment, {})

    def test_private_configuration_fills_only_missing_values(self):
        self.write(
            "GWAY_AGENT_WORKSPACE_ROOT=/srv/workspaces\n"
            "GWAY_AGENT_ALLOWED_REPOS=arthexis/gway-agent-ops\n"
            "GWAY_AGENT_SEED_CODEX_HOME=/srv/codex\n")
        environment = {"GWAY_AGENT_ALLOWED_REPOS": "owner/explicit"}
        applied = load_runtime_environment(path=self.path, environ=environment)
        self.assertEqual(applied, ("GWAY_AGENT_WORKSPACE_ROOT", "GWAY_AGENT_SEED_CODEX_HOME"))
        self.assertEqual(environment["GWAY_AGENT_ALLOWED_REPOS"], "owner/explicit")
        self.assertEqual(environment["GWAY_AGENT_WORKSPACE_ROOT"], "/srv/workspaces")

    def test_rejects_nonprivate_or_unrecognized_configuration(self):
        self.write("GWAY_AGENT_WORKSPACE_ROOT=/srv/workspaces\n", mode=0o644)
        with self.assertRaises(ValueError):
            load_runtime_environment(path=self.path, environ={})
        self.write("UNSAFE=1\n")
        with self.assertRaises(ValueError):
            load_runtime_environment(path=self.path, environ={})

    def test_rejects_relative_runtime_path(self):
        self.write("GWAY_AGENT_WORKSPACE_ROOT=relative\n")
        with self.assertRaises(ValueError):
            load_runtime_environment(path=self.path, environ={})
