"""Read-only snapshot of the single issue admission slot."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .dispatch import DEFAULT_STATE
from .observation import observe

TERMINAL = frozenset({"finished", "codex_failed", "interrupted", "released"})


def status(*, state_dir: Path | None = None) -> dict:
    root = Path(state_dir or DEFAULT_STATE)
    idle = {"state": "idle", "available": True, "active_issue": None, "active_at": None}
    requests = root / "requests"
    if requests.is_symlink():
        raise ValueError("unsafe requests directory")
    if requests.exists() and not requests.is_dir():
        raise ValueError("invalid requests directory")
    outstanding = []
    if requests.exists():
        for path in sorted(requests.glob("*.json")):
            if path.is_symlink():
                raise ValueError("unsafe request path")
            record = json.loads(path.read_text(encoding="utf-8"))
            ref, at = record.get("issue"), record.get("at")
            if not isinstance(ref, str) or not isinstance(at, str):
                raise ValueError("invalid request journal")
            observed = observe(ref, at, state_dir=root)["state"]
            if observed not in TERMINAL:
                outstanding.append((ref, at, observed))
    if len(outstanding) > 1:
        return {"state": "uncertain", "available": False,
                "active_issue": None, "active_at": None}
    lock = root / "execution.lock"
    if lock.is_symlink():
        raise ValueError("unsafe execution lock")
    running = False
    if lock.exists():
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    if outstanding:
        ref, at, observed = outstanding[0]
        state = "running" if running else ("reserved" if observed == "requested" else "uncertain")
        return {"state": state, "available": False, "active_issue": ref, "active_at": at}
    if running:
        return {"state": "running", "available": False,
                "active_issue": None, "active_at": None}
    return idle
