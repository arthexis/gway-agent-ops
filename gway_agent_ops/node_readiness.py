"""Read-only, secret-free readiness assessment for a node-local AgentOps owner.

This module never adopts credentials, dispatches work, changes configuration,
or contacts Watchtower. Authentication probes report only success/failure.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


def private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and info.st_uid == os.getuid() and not (info.st_mode & 0o077))
    except OSError:
        return False


def auth_available(*, runner=subprocess.run) -> bool:
    if not shutil.which("gh"):
        return False
    try:
        result = runner(["gh", "auth", "status"], capture_output=True,
                        timeout=10, check=False,
                        env={k: v for k, v in os.environ.items()
                             if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def assess(*, home: Path | None = None, workspace: Path | None = None,
           runner=subprocess.run) -> dict:
    home = Path.home() if home is None else Path(home)
    workspace = (Path(os.environ["GWAY_AGENT_WORKSPACE_ROOT"])
                 if workspace is None and os.environ.get("GWAY_AGENT_WORKSPACE_ROOT")
                 else workspace)
    release = home / ".local/share/gway-agent-ops/current"
    manifest = Path("/etc/gway-mcp-providers/agentops.json")
    try:
        target = release.resolve(strict=True)
        release_valid = (target.name.isascii() and len(target.name) == 40
                         and all(ch in "0123456789abcdef" for ch in target.name)
                         and (target / "bin/agent-mcp").is_file())
    except (OSError, RuntimeError):
        release_valid = False
    # Do not follow a potentially malicious provider-manifest symlink.
    try:
        mi = manifest.lstat()
        manifest_present = stat.S_ISREG(mi.st_mode) and mi.st_uid == 0
    except OSError:
        manifest_present = False
    return {
        "schema": 1,
        "node": os.uname().nodename,
        "owner_uid": os.getuid(),
        "release_present": release_valid,
        "provider_manifest_present": manifest_present,
        "github_authenticated": auth_available(runner=runner),
        "codex_executable_present": shutil.which("codex") is not None,
        "workspace_configured": workspace is not None,
        "workspace_private": private_directory(Path(workspace)) if workspace else False,
        "scheduling_checked": False,
        "execution_verified": False,
    }


def main() -> None:
    print(json.dumps(assess(), sort_keys=True))


if __name__ == "__main__":
    main()
