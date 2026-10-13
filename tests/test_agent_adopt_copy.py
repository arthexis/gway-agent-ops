"""A2.2 copy-based adoption and credential opt-in tests."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from gway_agent_ops.adopt import adopt


class AdoptCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "agent"
        (self.source / "config.toml").write_text(
            'model = "o4-mini"\nmodel_reasoning_effort = "medium"\n'
            'approval_policy = "on-request"\n'
            '[mcp_servers.private]\ncommand = "dangerous-command"\n',
            encoding="utf-8")
        auth = self.source / "auth.json"
        auth.write_text('{"secret":"DO-NOT-LOG"}', encoding="utf-8")
        auth.chmod(0o600)

    def test_adoption_only_copies_allowlisted_config(self):
        result = adopt(source=self.source, destination=self.target)
        self.assertEqual(result["files"], {"config.toml": "written"})
        self.assertFalse(result["auth_copied"])
        saved = (self.target / "config.toml").read_text()
        self.assertEqual(tomllib.loads(saved)["model"], "o4-mini")
        self.assertNotIn("dangerous-command", saved)
        self.assertNotIn("DO-NOT-LOG", saved)
        self.assertFalse((self.target / "auth.json").exists())
        self.assertFalse((self.target / "config.toml").is_symlink())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.target / "config.toml").stat().st_mode), 0o600)
        self.assertEqual(adopt(source=self.source, destination=self.target)["files"],
                         {"config.toml": "unchanged"})

    def test_changes_refuse_overwrite_without_force(self):
        adopt(source=self.source, destination=self.target)
        (self.source / "config.toml").write_text('model = "different"\n', encoding="utf-8")
        with self.assertRaises(FileExistsError):
            adopt(source=self.source, destination=self.target)
        self.assertIn("o4-mini", (self.target / "config.toml").read_text())
        self.assertEqual(adopt(source=self.source, destination=self.target, force=True)["files"],
                         {"config.toml": "written"})

    def test_auth_needs_opt_in_and_restrictive_permissions(self):
        (self.source / "auth.json").chmod(0o644)
        with self.assertRaises(ValueError):
            adopt(source=self.source, destination=self.target, include_auth=True)
        self.assertFalse(self.target.exists())
        (self.source / "auth.json").chmod(0o600)
        result = adopt(source=self.source, destination=self.target, include_auth=True)
        self.assertTrue(result["auth_copied"])
        self.assertEqual((self.target / "auth.json").read_text(), '{"secret":"DO-NOT-LOG"}')
        self.assertEqual(stat.S_IMODE((self.target / "auth.json").stat().st_mode), 0o600)

    def test_symlink_config_refused(self):
        config = self.source / "config.toml"
        backing = self.root / "backing.toml"
        config.rename(backing)
        config.symlink_to(backing)
        with self.assertRaises(ValueError):
            adopt(source=self.source, destination=self.target)
        self.assertFalse(self.target.exists())
