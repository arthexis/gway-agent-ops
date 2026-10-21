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
import stat

from .dispatch import DEFAULT_STATE, ISSUE, _private_directory, timestamp
from .github_issues import resolve
from .observation import observe


_TERMINAL = frozenset({"finished", "codex_failed", "interrupted", "released"})

def _launch_prerequisites() -> str:
    """Validate stable launch inputs *before* a durable request is recorded.

    This cannot prove the transient unit will start: failures after dispatch
    remain launch_unconfirmed to preserve at-most-once submission.
    """
    executable = shutil.which("agent")
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError("agent_cli_not_found")
    workspace = os.environ.get("GWAY_AGENT_WORKSPACE_ROOT", "")
    if not workspace or not Path(workspace).is_absolute():
        raise ValueError("agent_workspace_not_configured")
    if Path(workspace).is_symlink():
        raise ValueError("agent_workspace_unsafe")
    try:
        info = Path(workspace).stat()
    except OSError as exc:
        raise ValueError("agent_workspace_unavailable") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError("agent_workspace_unsafe")
    if not shutil.which("systemd-run"):
        raise ValueError("agent_systemd_run_unavailable")
    if not os.environ.get("XDG_RUNTIME_DIR"):
        raise ValueError("agent_user_bus_unavailable")
    return executable



def _outstanding_request(requests: Path, state_dir: Path, current: str) -> str | None:
    """Fail closed while any different durable submission is unresolved."""
    for path in sorted(requests.glob("*.json")):
        if path.is_symlink():
            raise ValueError("unsafe_dispatch_path")
        if path.stem == current:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        ref, at = record.get("issue"), record.get("at")
        if not isinstance(ref, str) or not isinstance(at, str):
            raise ValueError("invalid request journal")
        state = observe(ref, at, state_dir=state_dir)["state"]
        if state not in _TERMINAL:
            return ref
    return None



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
        execution_lock = root / "execution.lock"
        if execution_lock.is_symlink():
            raise ValueError("unsafe execution lock")
        execution_fd = os.open(execution_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(execution_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"issue": ref, "at": at, "state": "busy"}
        finally:
            os.close(execution_fd)
        # The submission gate is held here; do not call the status snapshot,
        # which would interpret our own reservation as busy.
        ledger = root / "accepted.jsonl"
        if ledger.is_symlink():
            raise ValueError("unsafe dispatch journal")
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                accepted = json.loads(line)
                identity = (accepted.get("issue"), accepted.get("at"))
                if not all(isinstance(value, str) for value in identity):
                    raise ValueError("invalid acceptance journal")
                if observe(*identity, state_dir=root)["state"] not in _TERMINAL:
                    return {"issue": ref, "at": at, "state": "busy",
                            "active_issue": identity[0]}
        active = _outstanding_request(requests, root, digest)
        if active is not None:
            return {"issue": ref, "at": at, "state": "busy", "active_issue": active}
        # Apply the existing GitHub trust gate before creating any work.
        resolve(ref, allowed_repositories=allowed_repositories)
        executable = _launch_prerequisites()
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


def release(ref: str, at: str, *, state_dir: Path | None = None,
            runner=subprocess.run) -> dict:
    """Administratively retire a request after stopping its systemd unit.

    No automatic retry. A new submission requires a different timestamp.
    Fail closed if the unit cannot be confirmed inactive or execution is locked.
    """
    if not isinstance(ref, str) or not ISSUE.fullmatch(ref):
        raise ValueError("invalid_issue_ref")
    if not isinstance(at, str):
        raise ValueError("invalid_timestamp")
    at = timestamp(at)
    root = Path(state_dir or DEFAULT_STATE)
    _private_directory(root)
    requests = root / "requests"
    _private_directory(requests)
    digest = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
    marker = requests / (digest + ".json")
    if marker.is_symlink() or not marker.exists():
        raise ValueError("request_not_found")
    lock = requests / "submission.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_dispatch_path")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        record = json.loads(marker.read_text(encoding="utf-8"))
        if record.get("issue") != ref or record.get("at") != at:
            raise ValueError("invalid request journal")
        if observe(ref, at, state_dir=root)["state"] in _TERMINAL:
            return {"issue": ref, "at": at, "state": "already_terminal"}
        execution_lock = root / "execution.lock"
        if execution_lock.is_symlink():
            raise ValueError("unsafe execution lock")
        execution_fd = os.open(execution_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(execution_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("execution_active") from exc
            unit = "gway-agent-" + digest[:32] + ".service"
            # Stop first: a successful is-active check alone would race a pending start.
            stopped = runner(["systemctl", "--user", "stop", unit],
                             capture_output=True, text=True, check=False, timeout=10)
            if stopped.returncode != 0:
                raise ValueError("unit_stop_unconfirmed")
            checked = runner(["systemctl", "--user", "is-active", unit],
                             capture_output=True, text=True, check=False, timeout=10)
            if checked.returncode == 0 or checked.stdout.strip() not in ("inactive", "failed", "unknown"):
                raise ValueError("unit_inactive_unconfirmed")
            released = root / "released"
            _private_directory(released)
            path = released / (digest + ".json")
            if path.is_symlink():
                raise ValueError("unsafe release path")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"issue": ref, "at": at, "state": "released"}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(released, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"issue": ref, "at": at, "state": "released"}
        finally:
            os.close(execution_fd)
    finally:
        os.close(descriptor)
