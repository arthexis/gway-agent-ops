"""Read-only view of bounded AgentOps reservations and live work."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .dispatch import DEFAULT_STATE
from .capacity import capacity, locked, repo_lock, unresolved
from .observation import observe


def status(*, state_dir: Path | None = None) -> dict:
    root = Path(state_dir or DEFAULT_STATE)
    requests = root / "requests"
    gate = requests / "submission.lock"
    gate_busy = locked(gate)
    active = unresolved(root)
    legacy_busy = locked(root / "execution.lock")
    # A lock held without a corresponding durable identity is ambiguous:
    # never declare that worker capacity safe to reuse.
    repo_busy = [row for row in active if locked(repo_lock(root, row[0]))]
    orphan_locks = False
    for path in root.glob("execution-repo-*.lock"):
        if locked(path) and not any(repo_lock(root, row[0]) == path for row in active):
            orphan_locks = True
    limit = capacity()
    count = len(active)
    if not active and not legacy_busy and not gate_busy and not orphan_locks:
        return {"state": "idle", "available": True,
                "active_issue": None, "active_at": None,
                "active_count": 0, "capacity": limit, "reservations": []}
    if legacy_busy or gate_busy or orphan_locks or count > limit:
        state = "uncertain"
    elif repo_busy:
        state = "running"
    elif all(row[2] == "requested" for row in active):
        state = "reserved"
    else:
        state = "uncertain"
    reservations = []
    for ref, at, observed in active:
        row = {"issue": ref, "at": at, "state": observed}
        # Retain old admission fields. Progress is optional and can never
        # override an accepted/terminal identity or a repository lock.
        progress = observe(ref, at, state_dir=root).get("progress")
        if progress is not None:
            row["progress"] = progress
        reservations.append(row)
    return {"state": state,
            "available": (not legacy_busy and not gate_busy
                          and not orphan_locks and count < limit),
            "active_issue": active[0][0] if count == 1 else None,
            "active_at": active[0][1] if count == 1 else None,
            "active_count": count, "capacity": limit,
            "reservations": reservations}
