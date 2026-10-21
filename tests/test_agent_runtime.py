"""A2.3 runtime preparation: no ambient credential inheritance."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.runtime import runtime_context


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "agent"
        self.root.mkdir(mode=0o700)
        self.codex = self.root / "codex-home"
        self.codex.mkdir(mode=0o700)
        self.config = self.codex / "config.toml"
        self.config.write_text('model = "test"\n')
        self.config.chmod(0o600)

    def test_explicit_codex_home_and_no_ambient_tokens(self):
        context = runtime_context(root=self.root, env={
            "PATH": "/usr/bin", "LANG": "C.UTF-8",
            "GITHUB_TOKEN": "secret", "OPENAI_API_KEY": "secret",
            "CODEX_HOME": "/operator/auth", "PYTHONPATH": "/unsafe"})
        self.assertEqual(context["environment"]["CODEX_HOME"], str(self.codex))
        self.assertEqual(context["environment"]["PATH"], "/usr/bin")
        for key in ("GITHUB_TOKEN", "OPENAI_API_KEY", "PYTHONPATH"):
            self.assertNotIn(key, context["environment"])
        self.assertFalse(context["authentication_available"])
        self.assertEqual(context["isolation"], "environment-only")

    def test_rejects_world_readable_config(self):
        self.config.chmod(0o644)
        with self.assertRaises(PermissionError):
            runtime_context(root=self.root)

    def test_rejects_symlinked_auth(self):
        (self.codex / "auth.json").symlink_to(self.config)
        with self.assertRaises(ValueError):
            runtime_context(root=self.root)

    def test_rejects_open_directory_permissions(self):
        self.codex.chmod(0o755)
        with self.assertRaises(PermissionError):
            runtime_context(root=self.root)

    def test_accepts_protected_auth_without_accessing_contents(self):
        auth = self.codex / "auth.json"
        auth.write_text('{"secret":"unread"}')
        auth.chmod(0o600)
        self.assertTrue(runtime_context(root=self.root)["authentication_available"])

    def test_explicit_seed_reference_reuses_private_home_without_copy(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed"
            seed.mkdir(mode=0o700)
            (seed / "config.toml").write_text('model = "seed"\\n')
            (seed / "config.toml").chmod(0o600)
            (seed / "auth.json").write_text(json.dumps({"auth": "seed"}))
            (seed / "auth.json").chmod(0o600)
            context = runtime_context(root=self.root, env={
                "GWAY_AGENT_SEED_CODEX_HOME": str(seed)})
            self.assertEqual(context["environment"]["CODEX_HOME"], str(seed))
            self.assertEqual(context["isolation"], "same-user-seed-reference")
            self.assertTrue(context["authentication_available"])
            self.assertFalse((self.codex / "auth.json").exists())
            seed.chmod(0o755)
            with self.assertRaises(PermissionError):
                runtime_context(root=self.root, env={
                    "GWAY_AGENT_SEED_CODEX_HOME": str(seed)})
