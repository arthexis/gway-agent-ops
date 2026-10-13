"""A2.4 end-to-end non-executing readiness and interrupted-write tests."""
import json
import stat
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.adopt import adopt
from gway_agent_ops.verify import verify


class AdoptionVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "config.toml").write_text('model = "test"\n')
        self.target = self.root / "agent" / "codex-home"

    def test_adoption_round_trip_and_idempotence(self):
        self.assertFalse(verify(root=self.target.parent)["ready"])
        self.assertEqual(adopt(source=self.source, destination=self.target)["files"]["config.toml"], "written")
        report = verify(root=self.target.parent)
        self.assertTrue(report["ready"])
        self.assertFalse(report["authentication_available"])
        self.assertEqual(adopt(source=self.source, destination=self.target)["files"]["config.toml"], "unchanged")
        self.assertTrue(verify(root=self.target.parent)["ready"])

    def test_partial_staging_file_does_not_override_installed_config(self):
        adopt(source=self.source, destination=self.target)
        (self.target / ".agent-adopt-aborted.tmp").write_text("partial secret")
        self.assertTrue(verify(root=self.target.parent)["ready"])
        self.assertEqual(adopt(source=self.source, destination=self.target)["files"]["config.toml"], "unchanged")
        self.assertEqual((self.target / "config.toml").read_text(), 'model = "test"\n')

    def test_credential_format_and_permissions(self):
        adopt(source=self.source, destination=self.target)
        auth = self.target / "auth.json"
        auth.write_text('{"secret":"never-print-me"}')
        auth.chmod(0o600)
        self.assertTrue(verify(root=self.target.parent)["ready"])
        auth.write_text("malformed JSON never-print-me")
        failure = verify(root=self.target.parent)
        self.assertFalse(failure["ready"])
        self.assertNotIn("never-print-me", json.dumps(failure))
        auth.write_text("{}")
        auth.chmod(0o644)
        self.assertEqual(verify(root=self.target.parent)["issues"], ["permissions"])

    def test_invalid_config_and_symlink_rejected(self):
        adopt(source=self.source, destination=self.target)
        config = self.target / "config.toml"
        config.write_text('not valid = =')
        self.assertFalse(verify(root=self.target.parent)["ready"])
        config.unlink()
        config.symlink_to(self.source / "config.toml")
        self.assertFalse(verify(root=self.target.parent)["ready"])
