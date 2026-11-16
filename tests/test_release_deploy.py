"""Focused tests for the owner-managed release activation contract."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gway_agent_ops import release_deploy as deployment


class CanonicalDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.sha = "a" * 40
        self.old_sha = "b" * 40
        self.root = self.home / ".local/share/gway-agent-ops"
        self.entry = self.home / ".local/bin/agent"
        self.releases = self.root / "releases"
        self.releases.mkdir(parents=True)
        self.entry.parent.mkdir(parents=True)
        for sha in (self.sha, self.old_sha):
            exe = self.releases / sha / "bin/agent"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        self.entry.symlink_to(self.releases / self.old_sha / "bin/agent")
        self.source = self.home / "source"
        self.source.mkdir()
        templates = self.source / "packaging/systemd"
        templates.mkdir(parents=True)
        for name in deployment.SCHEDULER_UNITS:
            (templates / name).write_text("[Unit]\\nDescription=Owner scheduler fixture\\n")

    def install(self, failing=None):
        def run(argv, **kwargs):
            if argv == [str(self.entry), failing]:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(deployment.subprocess, "check_output", return_value=self.sha+"\n"):
            return deployment.install(self.source, expected_sha=self.sha, home=self.home, run=run)

    def test_success_creates_current_previous_and_history_without_deleting(self):
        # Release directories already exist, so no real venv or pip is invoked.
        self.install()
        self.assertEqual(self.entry.resolve(), self.releases / self.sha / "bin/agent")
        self.assertEqual((self.root / "current").resolve(), self.releases / self.sha)
        self.assertEqual((self.root / "previous").resolve(), self.releases / self.old_sha)
        self.assertEqual([r["sha"] for r in deployment._read_history(self.root)], [self.sha])
        self.assertTrue((self.releases / self.old_sha).exists())

    def test_scheduler_units_are_installed_idempotently_without_activation(self):
        self.install()
        units = self.home / ".config/systemd/user"
        for name in deployment.SCHEDULER_UNITS:
            self.assertEqual((units / name).read_text(),
                             "[Unit]\\nDescription=Owner scheduler fixture\\n")
        before = {name: (units / name).stat().st_ino
                  for name in deployment.SCHEDULER_UNITS}
        self.install()
        self.assertEqual(before, {name: (units / name).stat().st_ino
                                  for name in deployment.SCHEDULER_UNITS})
        self.assertFalse((units / "timers.target.wants").exists())

    def test_scheduler_units_reject_symlink_and_restore_cli(self):
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / deployment.SCHEDULER_UNITS[0]).symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.entry.resolve(), self.releases / self.old_sha / "bin/agent")
        self.assertFalse((self.root / "current").exists())

    def test_failed_installed_probe_restores_prior_link(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.install(failing="--version")
        self.assertEqual(self.entry.resolve(), self.releases / self.old_sha / "bin/agent")
        self.assertFalse((self.root / "current").exists())
        self.assertFalse((self.root / deployment.HISTORY).exists())

    def test_rejects_unapproved_revision_before_mutation(self):
        with self.assertRaises(ValueError):
            self.install_revision("c" * 40)

    def install_revision(self, sha):
        with patch.object(deployment.subprocess, "check_output", return_value=self.sha+"\n"):
            return deployment.install(self.source, expected_sha=sha, home=self.home)

    def test_unsafe_activation_does_not_overwrite(self):
        self.entry.unlink()
        self.entry.symlink_to(self.home / "source")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.entry.resolve(), self.source)


if __name__ == "__main__":
    unittest.main()
