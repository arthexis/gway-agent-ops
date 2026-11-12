"""Bounded, authenticated cross-repository GitHub issue discovery.

Search provides a read-only candidate view, never execution authorization.
Submit and execute always re-fetch the exact issue through github_issues.resolve.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import urlencode

from .github_issues import IssueRejected, REPOSITORY

SEARCH_LABELS = frozenset({"gway-001", "local-agent"})
PAGE_SIZE = 100
MAX_PAGES = 3


def search_issues(*, label: str, creator: str = "arthexis",
                  runner=subprocess.run) -> list[dict]:
    if label not in SEARCH_LABELS or creator != "arthexis":
        raise IssueRejected("invalid_issue_search")
    query = urlencode({"q": f"is:issue is:open author:{creator} label:{label}",
                       "per_page": PAGE_SIZE})
    rows, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        argv = ["gh", "api", f"search/issues?{query}&page={page}"]
        try:
            result = runner(argv, capture_output=True, text=True, timeout=30,
                            check=False, env={k: v for k, v in os.environ.items()
                                              if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IssueRejected("github_unavailable") from exc
        if result.returncode:
            raise IssueRejected("github_unavailable")
        try:
            payload = json.loads(result.stdout)
            if (not isinstance(payload, dict) or type(payload.get("total_count")) is not int
                    or type(payload.get("incomplete_results")) is not bool
                    or not isinstance(payload.get("items"), list)):
                raise ValueError("invalid search response")
            if (payload["total_count"] < 0 or payload["incomplete_results"]
                    or payload["total_count"] > PAGE_SIZE * MAX_PAGES):
                raise IssueRejected("issue_search_incomplete")
            if payload["total_count"] < len(rows) + len(payload["items"]):
                raise ValueError("search count mismatch")
            for item in payload["items"]:
                if not isinstance(item, dict) or "pull_request" in item:
                    raise ValueError("unexpected search result")
                url = item.get("repository_url")
                match = re.fullmatch(r"https://api[.]github[.]com/repos/([^/]+)/([^/]+)", url or "")
                if not match:
                    raise ValueError("invalid repo URL")
                repo = f"{match.group(1)}/{match.group(2)}"
                if (not REPOSITORY.fullmatch(repo)
                        or any(s in (".", "..") for s in repo.split("/"))
                        or item.get("state") != "open"
                        or item.get("user", {}).get("login", "").casefold() != creator
                        or type(item.get("number")) is not int or item["number"] <= 0
                        or type(item.get("comments")) is not int or item["comments"] < 0
                        or not isinstance(item.get("title"), str)):
                    raise ValueError("untrusted search result")
                labels = item.get("labels")
                if (not isinstance(labels, list)
                        or any(not isinstance(x, dict) or not isinstance(x.get("name"), str)
                               for x in labels)):
                    raise ValueError("invalid labels")
                names = [x["name"] for x in labels]
                if label not in names:
                    raise ValueError("missing requested label")
                key = (repo, item["number"])
                if key in seen:
                    raise ValueError("duplicate issue")
                seen.add(key)
                rows.append({"repository": repo, "number": item["number"],
                             "title": item["title"], "state": "open",
                             "creator": creator, "comments": item["comments"],
                             "labels": names})
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise IssueRejected("github_unavailable") from exc
        if len(rows) == payload["total_count"]:
            return rows
        if not payload["items"]:
            raise IssueRejected("issue_search_incomplete")
    raise IssueRejected("issue_search_incomplete")
