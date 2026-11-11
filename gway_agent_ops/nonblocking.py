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
from .runtime import _same_user_runtime_dir, systemd_user_environment


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
    # Node-owned non-login execution contexts may not export this variable
    # even when their own /run/user/UID/bus is already available. Validate
    # that socket instead of requiring a particular shell environment.
    if _same_user_runtime_dir() is None:
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
           state_dir: Path | None = None, runner=subprocess.run,
           source: str = "direct-cli", trigger: str = "cli-submit") -> dict:
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
        from .github_issues import require_target_label
        require_target_label(resolve(ref, allowed_repositories=allowed_repositories))
        executable = _launch_prerequisites()
        request_record = {"issue": ref, "at": at, "state": "requested",
                          "source": source, "trigger": trigger}
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(request_record, handle)
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
            "--setenv=GWAY_AGENT_SEED_CODEX_HOME=" + os.environ.get("GWAY_AGENT_SEED_CODEX_HOME", ""),
            "--",
        ]
        # Invoke the same installed CLI used for read-only operations.
        command.extend([executable, "issue", ref, "--at", at, "--json"])
        # subprocess.run normally inherits its caller environment. Preserve
        # that behavior but select the validated *same-user* systemd bus,
        # never an arbitrary supplied DBus address or XDG path.
        launch_env = dict(os.environ)
        launch_env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        user_runtime = _same_user_runtime_dir()
        if user_runtime is None:
            launch_env.pop("XDG_RUNTIME_DIR", None)
        else:
            launch_env["XDG_RUNTIME_DIR"] = user_runtime
        try:
            result = runner(command, capture_output=True, text=True,
                            check=False, timeout=5, env=launch_env)
            if result.returncode == 0:
                return {"issue": ref, "at": at, "state": "requested"}
        except (OSError, subprocess.TimeoutExpired):
            pass
        # Persist the uncertain outcome; never retry the reserved identity.
        from .reporting import _write
        _write(marker, {**request_record, "state": "launch_unconfirmed"})
        return {"issue": ref, "at": at, "state": "launch_unconfirmed"}
    finally:
        os.close(descriptor)


def release(ref: str, at: str, *, state_dir: Path | None = None,
            runner=subprocess.run) -> dict:
    """Administratively retire a request after stopping its systemd unit.

    No automatic retry. A new submission requires a different timestamp.
    An explicit operator release retires a stopped Codex worker without
    claiming that independently managed services or effects have stopped.
    Require a free execution lock and verified systemd stop.
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
            # Codex may have persisted its terminal result while we waited
            # for execution.lock. Do not stop or relabel a completed process.
            if observe(ref, at, state_dir=root)["state"] in _TERMINAL:
                return {"issue": ref, "at": at, "state": "already_terminal"}
            # Validate the acceptance journal while holding both locks.
            # Release is an explicit operator reconciliation of Codex, not
            # an assertion that external administrative jobs have ended.
            was_accepted = False
            ledger = root / "accepted.jsonl"
            if ledger.is_symlink():
                raise ValueError("unsafe dispatch journal")
            if ledger.exists():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    accepted = json.loads(line)
                    if (type(accepted) is not dict
                            or not isinstance(accepted.get("issue"), str)
                            or not isinstance(accepted.get("at"), str)):
                        raise ValueError("invalid acceptance journal")
                    if accepted["issue"] == ref and accepted["at"] == at:
                        was_accepted = True
            # The same UID's existing systemd bus must be available before
            # operator reconciliation changes any journal state.
            user_env = systemd_user_environment()
            unit = "gway-agent-" + digest[:32] + ".service"
            # Stop first: a successful is-active check alone would race a pending start.
            stopped = runner(["systemctl", "--user", "stop", unit],
                             capture_output=True, text=True, check=False, timeout=10, env=user_env)
            missing_verified = False
            if stopped.returncode != 0:
                # --collect may have garbage-collected the transient unit.
                # A failed stop is safe only when systemd explicitly says the
                # computed unit is absent; ambiguous failures remain blocked.
                metadata = runner(
                    ["systemctl", "--user", "show", unit,
                     "--property=LoadState", "--property=ActiveState"],
                    capture_output=True, text=True, check=False, timeout=10, env=user_env)
                properties = dict(
                    line.split("=", 1) for line in metadata.stdout.splitlines()
                    if "=" in line)
                if (metadata.returncode != 0 or
                        properties.get("LoadState") != "not-found" or
                        properties.get("ActiveState") != "inactive"):
                    raise ValueError("unit_stop_unconfirmed")
                missing_verified = True
            checked = runner(["systemctl", "--user", "is-active", unit],
                             capture_output=True, text=True, check=False, timeout=10, env=user_env)
            state = checked.stdout.strip()
            if checked.returncode == 0:
                raise ValueError("unit_inactive_unconfirmed")
            if state == "unknown":
                # A collected transient unit can be absent after a verified
                # failed stop. Otherwise require independent systemd evidence;
                # a bare unknown must never imply inactivity.
                if not missing_verified:
                    metadata = runner(
                        ["systemctl", "--user", "show", unit,
                         "--property=LoadState", "--property=ActiveState"],
                        capture_output=True, text=True, check=False, timeout=10, env=user_env)
                    properties = dict(
                        line.split("=", 1) for line in metadata.stdout.splitlines()
                        if "=" in line)
                    if (metadata.returncode != 0 or properties.get("LoadState") != "not-found"
                            or properties.get("ActiveState") != "inactive"):
                        raise ValueError("unit_inactive_unconfirmed")
            elif state not in ("inactive", "failed"):
                raise ValueError("unit_inactive_unconfirmed")
            # The accepted Codex result is uncertain. fsync an interruption
            # report before writing the release marker. A failure here keeps
            # the old accepted slot blocked. This does not attest host work.
            if was_accepted:
                from .reporting import enqueue
                enqueue(ref, at, state_dir=root, status="interrupted")
            # A never-accepted request is only a request cancellation.
            # The frozen timestamp must not be resubmitted in either case.
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
