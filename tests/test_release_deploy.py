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
            (templates / name).write_text("[Unit]\nDescription=Owner scheduler fixture\n")

    def install(self, failing=None):
        def run(argv, **kwargs):
            if argv == [str(self.entry), failing]:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(deployment.subprocess, "check_output", return_value=self.sha+"\n"):
            return deployment.install(self.source, expected_sha=self.sha, home=self.home, run=run)

    def _committed_source(self):
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.source), *args], text=True).strip()
        git("init", "-q")
        git("config", "user.name", "Isolated fixture")
        git("config", "user.email", "fixture@example.invalid")
        (self.source / "pyproject.toml").write_text("[build-system]\n")
        git("add", ".")
        git("commit", "-qm", "Approved source")
        return git, git("rev-parse", "HEAD")

    def test_new_release_and_repeat_keep_pinned_checkout_clean(self):
        git, sha = self._committed_source()
        builds = []
        destination = self.releases / sha

        def run(argv, **kwargs):
            if argv[1:3] == ["-m", "venv"]:
                (destination / "bin").mkdir(parents=True)
            elif argv[0] == str(destination / "bin/pip"):
                staged = Path(argv[-1])
                builds.append(staged)
                self.assertNotEqual(staged, self.source)
                self.assertEqual(kwargs["cwd"], staged)
                self.assertEqual((staged / "pyproject.toml").read_bytes(),
                                 (self.source / "pyproject.toml").read_bytes())
                self.assertFalse((staged / ".git").exists())
                (staged / "build").mkdir()
                (staged / "build/generated.py").write_text("build side effect")
                exe = destination / "bin/agent"
                exe.write_text("#!/bin/sh\nexit 0\n")
                exe.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0)

        for _ in range(2):
            result = deployment.install(self.source, expected_sha=sha, home=self.home, run=run)
            self.assertEqual(result["installed_sha"], sha)
            self.assertEqual(git("status", "--porcelain", "--untracked-files=all"), "")
            self.assertEqual(self.entry.resolve(), destination / "bin/agent")
            self.assertFalse(list(self.root.glob(".build-*")))
        self.assertEqual(len(builds), 1)
        self.assertFalse(builds[0].exists())

    def test_failed_build_cleans_staging_not_source_or_previous_release(self):
        git, sha = self._committed_source()
        destination = self.releases / sha

        def run(argv, **kwargs):
            if argv[1:3] == ["-m", "venv"]:
                (destination / "bin").mkdir(parents=True)
            elif argv[0] == str(destination / "bin/pip"):
                staged = Path(argv[-1])
                (staged / "build").mkdir()
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)

        with self.assertRaises(subprocess.CalledProcessError):
            deployment.install(self.source, expected_sha=sha, home=self.home, run=run)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertFalse(list(self.root.glob(".build-*")))
        self.assertEqual(self.entry.resolve(), self.releases / self.old_sha / "bin/agent")

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
                             "[Unit]\nDescription=Owner scheduler fixture\n")
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
