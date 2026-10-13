"""Nonblocking, single-attempt systemd dispatch of trusted issues.

A durable request marker is written before launching. Repeated transport
requests never initiate another invocation, even after a restart. A launch
failure is intentionally ambiguous rather than silently retried.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil

from .dispatch import DEFAULT_STATE, ISSUE, _private_directory, timestamp
from .github_issues import resolve


def submit(ref: str, at: str, *, allowed_repositories: tuple[str, ...],
           state_dir: Path | None = None, runner=subprocess.run) -> dict:
    if not isinstance(ref, str) or not ISSUE.fullmatch(ref):
        raise ValueError("invalid_issue_ref")
    if not isinstance(at, str):
        raise ValueError("invalid_timestamp")
    at = timestamp(at)
    if not allowed_repositories:
        raise ValueError("repository_allowlist_missing")
    root = Path(state_dir or DEFAULT_STATE)
    _private_directory(root)
    requests = root / "requests"
    _private_directory(requests)
    digest = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
    marker = requests / (digest + ".json")
    lock = requests / "submission.lock"
    if lock.is_symlink() or marker.is_symlink():
        raise ValueError("unsafe_dispatch_path")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if marker.exists():
            return {"issue": ref, "at": at, "state": "already_requested"}
        # Apply the existing GitHub trust gate before creating any work.
        resolve(ref, allowed_repositories=allowed_repositories)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"issue": ref, "at": at, "state": "requested"}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(requests, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        # Systemd owns the lifetime, not the HTTP request or shell session.
        # A request marker is never removed on failure: outcome becomes unknown.
        command = [
            "systemd-run", "--user", "--quiet", "--collect",
            "--unit=gway-agent-" + digest[:32],
            "--setenv=GWAY_AGENT_ALLOWED_REPOS=" + ",".join(allowed_repositories),
            "--setenv=GWAY_AGENT_WORKSPACE_ROOT=" + os.environ.get("GWAY_AGENT_WORKSPACE_ROOT", ""),
            "--",
        ]
        # Invoke the same installed CLI used for read-only operations.
        executable = shutil.which("agent")
        if not executable or not Path(executable).is_file() or not os.environ.get("GWAY_AGENT_WORKSPACE_ROOT"):
            return {"issue": ref, "at": at, "state": "launch_unconfirmed"}
        command.extend([executable, "issue", ref, "--at", at, "--json"])
        try:
            result = runner(command, capture_output=True, text=True,
                            check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return {"issue": ref, "at": at, "state": "launch_unconfirmed"}
        return {"issue": ref, "at": at,
                "state": "requested" if result.returncode == 0 else "launch_unconfirmed"}
    finally:
        os.close(descriptor)
