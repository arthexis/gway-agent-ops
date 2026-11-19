"""Conservative recovery of stale, never-accepted systemd submissions.

No process is stopped automatically. A stale timestamp by itself grants no
authority. Only an absent, inactive, job-free systemd transient unit may be
retired using the existing idempotent release journal. Accepted Codex work
and externally managed administrative effects are never inferred complete.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess

from .capacity import unresolved
from .dispatch import DEFAULT_STATE
from .runtime import systemd_user_environment

PENDING_GRACE = timedelta(minutes=5)


def _absent_unit(ref: str, at: str, *, runner) -> bool:
    digest = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
    unit = "gway-agent-" + digest[:32] + ".service"
    keys = ("LoadState", "ActiveState", "Job", "MainPID",
            "ControlPID", "TasksCurrent", "ControlGroup")
    result = runner(
        ["systemctl", "--user", "show", unit,
         *("--property=" + key for key in keys)],
        capture_output=True, text=True, check=False, timeout=10,
        env=systemd_user_environment())
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return False
    props = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in keys or key in props:
            return False
        props[key] = value
    return (props.get("LoadState") == "not-found"
            and props.get("ActiveState") == "inactive"
            and props.get("Job") in ("0", "")
            and props.get("MainPID") == "0"
            and props.get("ControlPID") == "0"
            and props.get("TasksCurrent") in ("0", "[not set]")
            and props.get("ControlGroup") == "")


def reconcile_pending(*, state_dir: Path | None = None,
                      now: datetime | None = None,
                      runner=subprocess.run) -> dict:
    """Recover only stale never-accepted requests with proven absent workers.

    No retry and no release for accepted, running, unknown or malformed work.
    Other repositories can be admitted under the separate capacity policy.
    """
    root = Path(state_dir or DEFAULT_STATE)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("recovery requires timezone-aware UTC clock")
    retired = []
    unresolved_pending = []
    for ref, at, state in unresolved(root):
        if state not in ("requested", "launch_unconfirmed"):
            continue
        try:
            since = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid pending request timestamp") from None
        if since.tzinfo is None:
            raise ValueError("naive pending request timestamp")
        if current - since < PENDING_GRACE:
            continue
        if not _absent_unit(ref, at, runner=runner):
            unresolved_pending.append(ref)
            continue
        # release() acquires the normal submission gate, independently
        # revalidates non-running state and fsyncs the durable release record.
        # If a race makes the unit active, release fails closed.
        from .nonblocking import release
        try:
            outcome = release(ref, at, state_dir=root, runner=runner)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            unresolved_pending.append(ref)
            continue
        if outcome["state"] in ("released", "already_terminal"):
            retired.append(ref)
    return {"recovered": retired, "unresolved": unresolved_pending}
