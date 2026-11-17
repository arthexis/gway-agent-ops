"""Hermetic tests for non-mutating node readiness checks."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os

from gway_agent_ops import node_readiness


def test_private_directory_accepts_only_owner_private_regular_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    assert node_readiness.private_directory(workspace)
    workspace.chmod(0o755)
    assert not node_readiness.private_directory(workspace)
    workspace.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    assert not node_readiness.private_directory(alias)
    assert not node_readiness.private_directory(tmp_path / "absent")


def test_gh_status_never_discloses_output_or_inherits_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "sensitive")
    monkeypatch.setenv("GITHUB_TOKEN", "also-sensitive")
    with patch.object(node_readiness.shutil, "which", return_value="/usr/bin/gh"):
        def runner(argv, **kwargs):
            assert argv == ["gh", "auth", "status"]
            assert kwargs["capture_output"] is True
            assert "GH_TOKEN" not in kwargs["env"]
            assert "GITHUB_TOKEN" not in kwargs["env"]
            return SimpleNamespace(returncode=0, stderr=b"secret")
        assert node_readiness.auth_available(runner=runner)


def test_assess_reports_missing_auth_and_workspace_without_writes(tmp_path):
    with patch.object(node_readiness.shutil, "which", return_value=None):
        result = node_readiness.assess(
            home=tmp_path, workspace=tmp_path / "missing",
            runner=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not execute")))
    assert not result["github_authenticated"]
    assert not result["release_present"]
    assert not result["workspace_private"]
    assert not result["execution_verified"]
    assert not result["scheduling_checked"]


def test_release_directory_requires_pinned_revision(tmp_path):
    target = tmp_path / ".local/share/gway-agent-ops/releases" / ("a" * 40)
    exe = target / "bin/agent-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    current = tmp_path / ".local/share/gway-agent-ops/current"
    current.symlink_to(target, target_is_directory=True)
    with patch.object(node_readiness.shutil, "which", return_value=None):
        result = node_readiness.assess(home=tmp_path, workspace=None)
    assert result["release_present"]
