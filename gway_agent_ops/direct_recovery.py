"""Explicit recovery of directly invoked Codex with dedicated systemd evidence.

No Codex execution, no systemctl stop, no issue dispatch and no GitHub API here.
Unsandboxed, historical and collected/unverifiable direct workers stay blocked.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .dispatch import DEFAULT_STATE, ISSUE, _private_directory, timestamp
from .observation import observe

INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
TERMINAL = frozenset(("finished", "codex_failed", "interrupted", "released"))


def direct_unit(issue: str, at: str) -> str:
    """Unique systemd unit for the canonical issue/timestamp identity."""
    if not ISSUE.fullmatch(issue):
        raise ValueError("invalid_issue_ref")
    return "gway-agent-direct-" + hashlib.sha256(
        (issue + "\n" + at).encode("utf-8")).hexdigest()[:32] + ".service"


def direct_supervision(issue: str, at: str, *, invocation=None, cgroup_text=None):
    """Capture actual kernel cgroup membership, never trust a caller-provided unit.

    Invocations from ordinary shells or shared user services cannot later be
    reconciled safely. systemd-run's dedicated user service supplies both a
    unique INVOCATION_ID and /proc/self/cgroup path.
    """
    if invocation is None:
        invocation = os.environ.get("INVOCATION_ID", "")
    if type(invocation) is not str or not INVOCATION.fullmatch(invocation):
        return None
    if cgroup_text is None:
        try:
            cgroup_text = Path("/proc/self/cgroup").read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return None
    if type(cgroup_text) is not str:
        return None
    unit = direct_unit(issue, at)
    matches = [row[3:].strip() for row in cgroup_text.splitlines()
               if row.startswith("0::")]
    if len(matches) != 1:
        return None
    path = matches[0]
    if (not path.startswith("/") or not path.endswith("/" + unit)
            or ".." in path.split("/") or len(path) > 1024):
        return None
    return {"unit": unit, "invocation_id": invocation, "cgroup": path}


def _trusted_descriptor(path: Path) -> int:
    if path.is_symlink():
        raise ValueError("unsafe recovery lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        os.close(fd)
        raise ValueError("unsafe recovery lock")
    return fd


def _systemd_inactive(supervisor, *, runner):
    """Trust only the same systemd invocation with an empty controlled cgroup."""
    props = ("LoadState", "ActiveState", "SubState", "InvocationID",
             "KillMode", "Restart", "TasksCurrent", "MainPID", "ControlPID",
             "ControlGroup")
    result = runner(["systemctl", "--user", "show", supervisor["unit"],
                     *("--property=" + key for key in props)],
                    capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0 or not isinstance(result.stdout, str):
        raise ValueError("direct_supervisor_unavailable")
    parsed = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in props or key in parsed:
            raise ValueError("invalid_supervisor_evidence")
        parsed[key] = value
    required = {
        "LoadState": "loaded",
        "InvocationID": supervisor["invocation_id"], "KillMode": "control-group",
        "Restart": "no", "TasksCurrent": "0", "MainPID": "0", "ControlPID": "0",
    }
    if any(parsed.get(key) != value for key, value in required.items()):
        raise ValueError("direct_supervisor_not_inactive")
    # SIGKILL commonly leaves a failed/failed unit rather than inactive/dead.
    # Neither state proves anything alone: match the original invocation and
    # demand an empty, non-restarting cgroup before reporting interruption.
    if (parsed.get("ActiveState"), parsed.get("SubState")) not in (
            ("inactive", "dead"), ("failed", "failed")):
        raise ValueError("direct_supervisor_not_inactive")
    # systemd may clear ControlGroup when the service becomes inactive; the
    # matching InvocationID and empty TasksCurrent then bind the evidence to
    # the original unit. A different nonempty cgroup is always rejected.
    if parsed.get("ControlGroup") not in ("", supervisor["cgroup"]):
        raise ValueError("direct_supervisor_cgroup_changed")


def recover_direct(ref: str, at: str, *, state_dir: Path | None = None,
                   runner=subprocess.run) -> dict:
    """Inspect direct-CLI worker evidence; refuse release without containment proof."""
    if type(ref) is not str or not ISSUE.fullmatch(ref) or type(at) is not str:
        raise ValueError("invalid_issue_identity")
    at = timestamp(at)
    root = Path(state_dir or DEFAULT_STATE)
    _private_directory(root)
    requests = root / "requests"
    _private_directory(requests)
    digest = hashlib.sha256((ref + "\n" + at).encode("utf-8")).hexdigest()
    marker = requests / (digest + ".json")
    if marker.exists() or marker.is_symlink():
        raise ValueError("systemd_submitted_request_use_release")
    gate = _trusted_descriptor(requests / "submission.lock")
    try:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("admission_active") from exc
        execution = _trusted_descriptor(root / "execution.lock")
        try:
            try:
                fcntl.flock(execution, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("execution_active") from exc
            journal = root / "accepted.jsonl"
            if journal.is_symlink() or not journal.is_file():
                raise ValueError("acceptance_journal_unavailable")
            accepted = []
            for line in journal.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if type(record) is not dict:
                    raise ValueError("invalid_acceptance_journal")
                if record.get("issue") == ref and record.get("at") == at:
                    accepted.append(record)
            if len(accepted) != 1:
                raise ValueError("direct_acceptance_unknown")
            state = observe(ref, at, state_dir=root)["state"]
            if state in TERMINAL:
                return {"issue": ref, "at": at, "state": "already_terminal"}
            if state != "accepted":
                raise ValueError("direct_execution_state_unverified")
            supervisor = accepted[0].get("direct_supervisor")
            if type(supervisor) is not dict or set(supervisor) != {
                    "unit", "invocation_id", "cgroup"}:
                raise ValueError("direct_supervision_missing")
            # Revalidate immutable identity from the journal before contacting
            # systemd. Old records and arbitrary user sessions fail closed.
            validated = direct_supervision(ref, at,
                invocation=supervisor["invocation_id"],
                cgroup_text="0::" + str(supervisor["cgroup"]))
            if validated != supervisor or supervisor["unit"] != direct_unit(ref, at):
                raise ValueError("invalid_direct_supervisor")
            _systemd_inactive(supervisor, runner=runner)
            # A matching invocation and an empty original cgroup do NOT prove
            # the entire execution family stopped. A same-user process can
            # escape to a different systemd scope via the user manager and
            # outlive the original unit. The accepted journal has no durable
            # evidence of all escaped children and PID absence/reuse cannot
            # reconstruct that lineage. Do not enqueue an "interrupted"
            # terminal report or reopen admission based only on unit evidence.
            # A separately proven process-containment boundary is required;
            # this Draft deliberately does not implement or authorize one.
            raise ValueError("direct_containment_unverified")
        finally:
            os.close(execution)
    finally:
        os.close(gate)
