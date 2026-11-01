"""Agent Ops retention uses its canonical installer lock and preserves data."""
from pathlib import Path
import tempfile
import unittest
from gway_agent_ops import release_deploy


class AgentPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".local/share/gway-agent-ops"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.current, self.previous, self.old = ["a"*40, "b"*40, "c"*40]
        for name in (self.current, self.previous, self.old):
            exe = self.releases / name / "bin/agent"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        (self.root / "current").symlink_to(self.releases / self.current)
        (self.root / "previous").symlink_to(self.releases / self.previous)
        (self.bin / "agent").symlink_to(self.releases / self.current / "bin/agent")

    def test_dry_run_apply_and_data_preservation(self):
        history = self.root / release_deploy.HISTORY
        history.write_text('{"schema": 1, "verified": []}')
        preview = release_deploy.prune(home=self.home)
        self.assertEqual(preview["status"], "candidates_ready")
        self.assertTrue((self.releases / self.old).exists())
        self.assertEqual({x["name"] for x in preview["candidates"]}, {self.old})
        receipt = release_deploy.prune(home=self.home, apply=True)
        self.assertEqual(receipt["deleted"], [self.old])
        self.assertTrue((self.releases / self.current).exists())
        self.assertTrue((self.releases / self.previous).exists())
        self.assertTrue(history.exists())

    def test_protected_cli_and_previous(self):
        (self.bin / "agent").unlink()
        (self.bin / "agent").symlink_to(self.releases / self.old / "bin/agent")
        report = release_deploy.prune(home=self.home, apply=True)
        self.assertNotIn(self.old, report["deleted"])
        self.assertTrue((self.releases / self.old).exists())

    def test_unknown_name_blocks(self):
        (self.releases / ".staging-incomplete").mkdir()
        report = release_deploy.prune(home=self.home, apply=True)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
