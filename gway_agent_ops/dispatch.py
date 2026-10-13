"""Single-slot, timestamp-addressed dispatch admission (no execution).

The caller must validate GitHub trust *before* entering admission, and retain
the context until execution ends. This module never invokes Codex.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

ISSUE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
DEFAULT_STATE = Path.home() / ".local/share/gway-agent-ops/dispatch"


def timestamp(value: str | None = None, *, now: datetime | None = None) -> str:
    """Normalize to UTC; retain microseconds so retries compare consistently."""
    instant = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("dispatch timestamp requires a timezone")
    instant = instant.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if instant > current + timedelta(minutes=5):
        raise ValueError("dispatch timestamp is too far in the future")
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("dispatch path is not a regular directory")
    if info.st_uid != os.getuid() or (info.st_mode & 0o077):
        raise PermissionError("dispatch directory must be owned and private")


class Busy(Exception):
    """Another command currently holds the single execution slot."""


class Duplicate(Exception):
    """The same issue and timestamp was previously accepted."""


@contextlib.contextmanager
def admission(issue: str, at: str, *, state_dir: Path | None = None, comment_baseline: int | None = None):
    """Atomically deduplicate and accept under an exclusive one-shot lock.

    A previously accepted key always reports Duplicate, even after restart.
    The lock is retained until the caller leaves this context. No queue.
    """
    if not ISSUE.fullmatch(issue):
        raise ValueError("invalid issue identity")
    if comment_baseline is not None and (type(comment_baseline) is not int or comment_baseline < 0):
        raise ValueError("invalid comment baseline")
    at = timestamp(at)
    directory = Path(state_dir or DEFAULT_STATE)
    _private_directory(directory)
    requests = directory / "requests"
    _private_directory(requests)
    gate_path = requests / "submission.lock"
    if gate_path.is_symlink():
        raise ValueError("unsafe submission lock")
    gate_fd = os.open(gate_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_path = directory / "execution.lock"
    if lock_path.is_symlink():
        raise ValueError("unsafe lock path")
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_EX)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except BaseException:
        os.close(gate_fd)
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("unsafe lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("an agent invocation is already in progress") from exc
        from .nonblocking import _outstanding_request
        import hashlib
        digest = hashlib.sha256((issue + "\n" + at).encode()).hexdigest()
        active = _outstanding_request(requests, directory, digest)
        if active is not None:
            raise Busy("an unresolved issue request already owns the slot")
        log = directory / "accepted.jsonl"
        if log.is_symlink():
            raise ValueError("unsafe dispatch journal")
        fd = os.open(log, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("unsafe dispatch journal")
            with os.fdopen(fd, "r+", encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    if row["issue"] == issue and row["at"] == at:
                        raise Duplicate("this issue and timestamp were already accepted")
                record = {"issue": issue, "at": at}
                if comment_baseline is not None:
                    record["comment_baseline"] = comment_baseline
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # fdopen takes ownership of fd; already closed after context exit.
            raise
        fcntl.flock(gate_fd, fcntl.LOCK_UN)
        yield record
    finally:
        os.close(descriptor)
        os.close(gate_fd)
