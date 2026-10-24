"""Read-only dispatch observation from the durable acceptance and report journals."""
from __future__ import annotations

import json
import hashlib
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
    released = root / "released" / (hashlib.sha256((ref + "\n" + at).encode()).hexdigest() + ".json")
    if released.is_symlink():
        raise ValueError("unsafe release path")
    if released.exists():
        record = json.loads(released.read_text(encoding="utf-8"))
        if record.get("issue") != ref or record.get("at") != at:
            raise ValueError("invalid release journal")
        return {"issue": ref, "at": at, "state": "released", **_provenance(root, ref, at)}
    ledger = root / "accepted.jsonl"
    if not ledger.exists():
        return _pending_request(root, ref, at)
    if ledger.is_symlink():
        raise ValueError("unsafe dispatch journal")
    accepted = any(
        row.get("issue") == ref and row.get("at") == at
        for row in (json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines())
    )
    if not accepted:
        return _pending_request(root, ref, at)
    key = _key(ref, at) + ".json"
    for folder in ("outbox", "delivered"):
        path = root / folder / key
        if path.is_symlink():
            raise ValueError("unsafe report path")
        if path.exists():
            if folder == "delivered":
                report = json.loads(path.read_text(encoding="utf-8"))
                state = report.get("status")
                if state not in ("finished", "codex_failed", "interrupted"):
                    return {"issue": ref, "at": at, "state": "reported_unknown", **_provenance(root, ref, at)}
                return {"issue": ref, "at": at, "state": state,
                        "reported": True, "exit_code": report.get("exit_code"),
                        **_provenance(root, ref, at)}
            report = json.loads(path.read_text(encoding="utf-8"))
            return {"issue": ref, "at": at, "state": report["status"],
                    "exit_code": report.get("exit_code"), **_provenance(root, ref, at)}
    return {"issue": ref, "at": at, "state": "accepted", **_provenance(root, ref, at)}


def _pending_request(root: Path, ref: str, at: str) -> dict:
    digest = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
    path = root / "requests" / (digest + ".json")
    if path.is_symlink():
        raise ValueError("unsafe request path")
    if not path.exists():
        return {"issue": ref, "at": at, "state": "unknown"}
    request = json.loads(path.read_text(encoding="utf-8"))
    if request.get("issue") != ref or request.get("at") != at:
        raise ValueError("invalid request journal")
    return {"issue": ref, "at": at, "state": "requested", **_provenance(root, ref, at)}


def _provenance(root: Path, ref: str, at: str) -> dict:
    digest = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
    path = root / "requests" / (digest + ".json")
    if path.is_symlink() or not path.is_file():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("issue") != ref or record.get("at") != at:
        return {}
    return {key: record[key] for key in ("source", "trigger")
            if isinstance(record.get(key), str)}
