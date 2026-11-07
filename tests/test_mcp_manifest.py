from pathlib import Path

import pytest

from gway_agent_ops.mcp_manifest import manifest


def test_manifest_from_installed_release(tmp_path):
    release = tmp_path / ("a" * 40)
    exe = release / "bin" / "agent-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    result = manifest(release)
    assert result["source_id"] == "agentops"
    assert result["revision"] == "a" * 40
    assert result["argv"] == [str(exe)]


def test_missing_executable_is_rejected(tmp_path):
    release = tmp_path / ("a" * 40)
    release.mkdir()
    with pytest.raises(ValueError, match="missing"):
        manifest(release)
