"""Read-only local accepted execution history."""
import json
from pathlib import Path

from .dispatch import DEFAULT_STATE, ISSUE, timestamp
from .observation import observe


def history(*, limit=20, state_dir=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid history limit")
    root = Path(state_dir or DEFAULT_STATE)
    ledger = root / "accepted.jsonl"
    if ledger.is_symlink():
        raise ValueError("unsafe dispatch journal")
    if not ledger.exists():
        return []
    rows = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if (type(row) is not dict or not isinstance(row.get("issue"), str)
                or not ISSUE.fullmatch(row["issue"]) or not isinstance(row.get("at"), str)):
            raise ValueError("invalid acceptance journal")
        at = timestamp(row["at"])
        if at != row["at"]:
            raise ValueError("noncanonical acceptance timestamp")
        rows.append((at, row["issue"]))
    rows.sort(reverse=True)
    output = []
    for at, issue in rows[:limit]:
        result = observe(issue, at, state_dir=root)
        output.append({key: result.get(key) for key in (
            "issue", "at", "state", "exit_code", "delivery_attempted", "delivery_confirmed")})
    return output
