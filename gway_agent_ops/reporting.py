"""Durable GitHub reporting outbox; never repeats agent execution."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .dispatch import DEFAULT_STATE, _private_directory

ISSUE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")


def _key(issue: str, at: str) -> str:
    if not ISSUE.fullmatch(issue):
        raise ValueError("invalid issue")
    return hashlib.sha256((issue + "\n" + at).encode()).hexdigest()


def _write(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enqueue(issue: str, at: str, *, state_dir: Path | None = None,
            status: str = "interrupted", exit_code: int | None = None) -> dict:
    """Persist sanitized report prior to attempting network delivery."""
    root = Path(state_dir or DEFAULT_STATE)
    _private_directory(root)
    outbox = root / "outbox"
    _private_directory(outbox)
    identity = _key(issue, at)
    path = outbox / (identity + ".json")
    if path.is_symlink():
        raise ValueError("unsafe report path")
    if path.exists():
        return json.loads(path.read_text())
    if status not in ("interrupted", "finished", "codex_failed"):
        raise ValueError("invalid reporting status")
    record = {"issue": issue, "at": at, "status": status, "exit_code": exit_code,
              "marker": "agent-dispatch:" + identity}
    _write(path, record)
    return record


def _api(args: list[str], *, runner=subprocess.run):
    result = runner(["gh", "api", *args], capture_output=True, text=True,
                    timeout=25, check=False)
    if result.returncode:
        raise RuntimeError("github_reporting_unavailable")
    return json.loads(result.stdout)


def deliver(record: dict, *, runner=subprocess.run) -> None:
    """Idempotent against successfully posted but locally unacknowledged reports."""
    repository, number = record["issue"].rsplit("#", 1)
    endpoint = f"repos/{repository}/issues/{number}/comments"
    existing = _api([endpoint, "--paginate"], runner=runner)
    if not isinstance(existing, list):
        raise RuntimeError("github_reporting_invalid_response")
    if any(record["marker"] in str(row.get("body", "")) for row in existing if isinstance(row, dict)):
        return
    message = (f"Agent invocation {record['at']}: {record['status']}."
               + (f" Codex exit code: {record['exit_code']}." if record["exit_code"] is not None else "")
               + f"\n<!-- {record['marker']} -->")
    _api([endpoint, "-f", f"body={message}"], runner=runner)


def flush(*, state_dir: Path | None = None, runner=subprocess.run) -> dict:
    """Retry queued comments only. Failures remain durable for later invocation."""
    root = Path(state_dir or DEFAULT_STATE)
    outbox = root / "outbox"
    if not outbox.exists():
        return {"sent": 0, "pending": 0}
    sent = 0
    pending = 0
    for path in sorted(outbox.glob("*.json")):
        if path.is_symlink():
            pending += 1
            continue
        try:
            record = json.loads(path.read_text())
            deliver(record, runner=runner)
            path.unlink()
            sent += 1
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pending += 1
    return {"sent": sent, "pending": pending}


def recover(*, state_dir: Path | None = None) -> int:
    """Reconcile accepted invocations not yet given a reporting record.

    Must be called while the dispatch execution lock is held, so an active
    process cannot be misidentified as crashed.
    """
    root = Path(state_dir or DEFAULT_STATE)
    ledger = root / "accepted.jsonl"
    if not ledger.exists():
        return 0
    outbox = root / "outbox"
    _private_directory(outbox)
    created = 0
    for line in ledger.read_text().splitlines():
        accepted = json.loads(line)
        path = outbox / (_key(accepted["issue"], accepted["at"]) + ".json")
        # The next chunk can add a persistent delivered-ack index. For now
        # existing remotely posted comments are de-duplicated by marker.
        if not path.exists():
            enqueue(accepted["issue"], accepted["at"], state_dir=root)
            created += 1
    return created
