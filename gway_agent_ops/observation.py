"""Read-only dispatch observation from the durable acceptance and report journals."""
from __future__ import annotations

import json
from pathlib import Path

from .dispatch import DEFAULT_STATE, ISSUE, timestamp
from .reporting import _key


def observe(ref: str, at: str, *, state_dir: Path | None = None) -> dict:
    if not isinstance(ref, str) or not ISSUE.fullmatch(ref):
        raise ValueError("invalid_issue_ref")
    if not isinstance(at, str):
        raise ValueError("invalid_timestamp")
    at = timestamp(at)
    root = Path(state_dir or DEFAULT_STATE)
    ledger = root / "accepted.jsonl"
    if not ledger.exists():
        return {"issue": ref, "at": at, "state": "unknown"}
    if ledger.is_symlink():
        raise ValueError("unsafe dispatch journal")
    accepted = any(
        row.get("issue") == ref and row.get("at") == at
        for row in (json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines())
    )
    if not accepted:
        return {"issue": ref, "at": at, "state": "unknown"}
    key = _key(ref, at) + ".json"
    for folder in ("outbox", "delivered"):
        path = root / folder / key
        if path.is_symlink():
            raise ValueError("unsafe report path")
        if path.exists():
            if folder == "delivered":
                return {"issue": ref, "at": at, "state": "reported"}
            report = json.loads(path.read_text(encoding="utf-8"))
            return {"issue": ref, "at": at, "state": report["status"],
                    "exit_code": report.get("exit_code")}
    return {"issue": ref, "at": at, "state": "accepted"}
