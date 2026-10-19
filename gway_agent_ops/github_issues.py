"""Read-only GitHub issue resolution and strict operator trust boundary."""
from __future__ import annotations

import json
import os
import re
import subprocess

from .cli import parse_issue_ref

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class IssueRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def resolve(ref: str, *, repo: str | None = None, allowed_repositories: tuple[str, ...],
            creator: str = "arthexis", runner=subprocess.run) -> dict:
    """Fetch one issue by explicit repository allowlist, never execute issue text.

    Uses GitHub CLI's authenticated API with argument-vector subprocess only.
    Auth, network failures and unexpected payloads fail closed.
    """
    repository, number = parse_issue_ref(ref, repo)
    if repository not in allowed_repositories:
        raise IssueRejected("repository_not_allowed")
    try:
        result = runner(
            ["gh", "api", f"repos/{repository}/issues/{number}"],
            capture_output=True, text=True, timeout=20,
            env={k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IssueRejected("github_unavailable") from exc
    if result.returncode != 0:
        raise IssueRejected("github_unavailable")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or "pull_request" in payload:
            raise IssueRejected("not_an_issue")
        if payload["state"] != "open":
            raise IssueRejected("issue_not_open")
        if payload["user"]["login"].casefold() != creator.casefold():
            raise IssueRejected("creator_not_allowed")
        if payload["number"] != number:
            raise IssueRejected("issue_mismatch")
        if payload["repository_url"].rstrip("/").endswith(f"/repos/{repository}") is False:
            raise IssueRejected("repository_mismatch")
        if (not isinstance(payload["title"], str) or not isinstance(payload.get("body"), (str, type(None)))
                or type(payload.get("comments")) is not int or payload["comments"] < 0):
            raise IssueRejected("invalid_issue")
        labels = payload.get("labels", [])
        if not isinstance(labels, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str) for x in labels):
            raise IssueRejected("invalid_issue_labels")
        return {"labels": [x["name"] for x in labels], "repository": repository, "number": number,
                "title": payload["title"], "body": payload.get("body") or "",
                "url": f"https://github.com/{repository}/issues/{number}",
                "creator": payload["user"]["login"], "state": "open",
                "comments": payload["comments"]}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, IssueRejected):
            raise
        raise IssueRejected("invalid_github_response") from exc
