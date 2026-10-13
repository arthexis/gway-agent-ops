"""Read-only projection of trusted GitHub issues and dispatch history."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .contracts import issue_row
from .dispatch import DEFAULT_STATE
from .github_issues import IssueRejected


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


def list_issues(*, allowed_repositories: tuple[str, ...], state_dir: Path | None = None,
                runner=subprocess.run, creator: str = "arthexis") -> list[dict]:
    """List open, trusted issues. Fail closed rather than returning partial results."""
    if not allowed_repositories:
        raise IssueRejected("repository_allowlist_missing")
    history = _history(Path(state_dir or DEFAULT_STATE))
    rows = []
    for repository in sorted(set(allowed_repositories)):
        from .github_issues import REPOSITORY
        if not REPOSITORY.fullmatch(repository):
            raise IssueRejected("invalid_repository_allowlist")
        cmd = ["gh", "api", "--paginate", "--slurp",
               f"repos/{repository}/issues?state=open&per_page=100"]
        try:
            result = runner(cmd, capture_output=True, text=True, timeout=30,
                            check=False,
                            env={k: v for k, v in os.environ.items()
                                 if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
            if result.returncode:
                raise IssueRejected("github_unavailable")
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("unexpected GitHub listing")
            for page in pages:
                for item in page:
                    if not isinstance(item, dict) or "pull_request" in item:
                        continue
                    if item.get("state") != "open" or not isinstance(item.get("user"), dict):
                        continue
                    if item["user"].get("login", "").casefold() != creator.casefold():
                        continue
                    number = item.get("number")
                    count = item.get("comments")
                    if type(number) is not int or type(count) is not int or count < 0:
                        raise ValueError("invalid issue metadata")
                    key = f"{repository}#{number}"
                    accepted = history.get(key)
                    state = "open"
                    if accepted:
                        baseline = accepted.get("comment_baseline")
                        # A baseline is needed to infer attendance; older A3.2
                        # entries lack it, so never guess that a reply arrived.
                        state = ("attended" if type(baseline) is int and count > baseline
                                 else "sent")
                    rows.append(issue_row(repository=repository, number=number,
                                          title=item["title"], state=state,
                                          comments=count,
                                          sent_at=accepted["at"] if accepted else None))
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, IssueRejected):
                raise
            raise IssueRejected("github_unavailable") from exc
    return sorted(rows, key=lambda row: (row["repository"], row["number"]))
