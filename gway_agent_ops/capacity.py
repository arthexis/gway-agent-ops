"""Bounded per-node AgentOps capacity without erasing accepted identities.

Callers serialize admission through requests/submission.lock; this module only
reads immutable request and report evidence. A request still counts until
terminal evidence is durable, regardless of lock ownership or elapsed time.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from .observation import observe

TERMINAL = frozenset({"finished", "codex_failed", "interrupted", "released"})


def capacity() -> int:
    """Use all but one logical CPU, capped at three workers on gway-001."""
    return max(1, min(3, (os.cpu_count() or 2) - 1))


def repo_lock(root: Path, issue: str) -> Path:
    repo = issue.split("#", 1)[0].casefold()
    return root / ("execution-repo-" + hashlib.sha256(repo.encode()).hexdigest()[:24] + ".lock")


def locked(path: Path) -> bool:
    """Only the same owner's regular lock files may be inspected."""
    import stat
    if path.is_symlink():
        raise ValueError("unsafe execution lock")
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077):
            raise ValueError("unsafe execution lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def unresolved(root: Path) -> list[tuple[str, str, str]]:
    """Deduplicate request + acceptance records; invalid journals fail closed."""
    identities: dict[tuple[str, str], str] = {}
    requests = root / "requests"
    if requests.is_symlink() or (requests.exists() and not requests.is_dir()):
        raise ValueError("unsafe requests directory")
    if requests.exists():
        for path in sorted(requests.glob("*.json")):
            if path.is_symlink():
                raise ValueError("unsafe request journal")
            record = json.loads(path.read_text(encoding="utf-8"))
            ref, at = record.get("issue"), record.get("at")
            if not isinstance(ref, str) or not isinstance(at, str):
                raise ValueError("invalid request journal")
            state = observe(ref, at, state_dir=root)["state"]
            if state not in TERMINAL:
                identities[(ref, at)] = state
    journal = root / "accepted.jsonl"
    if journal.is_symlink():
        raise ValueError("unsafe acceptance journal")
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            ref, at = record.get("issue"), record.get("at")
            if not isinstance(ref, str) or not isinstance(at, str):
                raise ValueError("invalid acceptance journal")
            state = observe(ref, at, state_dir=root)["state"]
            if state not in TERMINAL:
                identities[(ref, at)] = state
    return [(ref, at, state) for (ref, at), state in sorted(identities.items())]
