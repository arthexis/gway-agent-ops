"""Read-only cross-repository projection of trusted, explicitly routed issues."""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import issue_row
from .dispatch import DEFAULT_STATE
from .github_search import search_issues


def _history(state_dir: Path) -> dict:
    ledger = state_dir / "accepted.jsonl"
    latest = {}
    if not ledger.exists():
        return latest
    if ledger.is_symlink():
        raise ValueError("unsafe dispatch journal")
    for line in ledger.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        identity = record["issue"]
        if identity not in latest or record["at"] > latest[identity]["at"]:
            latest[identity] = record
    return latest


def list_issues(*, allowed_repositories: tuple[str, ...] | None = None,
                state_dir: Path | None = None, runner=None,
                creator: str = "arthexis") -> list[dict]:
    """Search GitHub, never the deploy-target inventory. The old arg is ignored.

    Search is bounded. Incomplete or invalid responses fail closed; no partial
    list is presented as complete. Discovery is not execution authorization.
    """
    history = _history(Path(state_dir or DEFAULT_STATE))
    opts = {"label": "gway-001", "creator": creator}
    if runner is not None:
        opts["runner"] = runner
    rows = []
    for item in search_issues(**opts):
        repository, number = item["repository"], item["number"]
        key = f"{repository}#{number}"
        accepted = history.get(key)
        state = "open"
        if accepted:
            baseline = accepted.get("comment_baseline")
            state = ("attended" if type(baseline) is int and item["comments"] > baseline
                     else "sent")
        rows.append(issue_row(repository=repository, number=number,
                              title=item["title"], state=state,
                              comments=item["comments"],
                              sent_at=accepted["at"] if accepted else None,
                              labels=item["labels"], eligible=True,
                              ineligible_reason=None))
    return sorted(rows, key=lambda row: (row["repository"], row["number"]))
