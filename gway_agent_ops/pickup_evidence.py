"""Bounded read-only evidence retrieval for automatic pickup (#126)."""
import json
import os
import subprocess

from .github_issues import IssueRejected
from .dispatch import ISSUE
from .pickup_policy import NODE

PAGE_SIZE = 100
MAX_PAGES = 3


def label_events(ref, *, runner=subprocess.run):
    """Return complete issue event pages, never a partial authorization view.

    Pagination is deliberately bounded and fails closed if completeness cannot
    be demonstrated. Callers must resolve the current issue again before claim.
    """
    if not isinstance(ref, str) or not ISSUE.fullmatch(ref):
        raise IssueRejected("invalid_issue_ref")
    repository, number = ref.split("#")
    if any(part in (".", "..") for part in repository.split("/")):
        raise IssueRejected("invalid_issue_ref")
    rows = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        try:
            response = runner(
                ["gh", "api", "--hostname", "github.com",
                 f"repos/{repository}/issues/{number}/events?per_page={PAGE_SIZE}&page={page}"],
                capture_output=True, text=True, timeout=20, check=False,
                env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")},
            )
            if response.returncode:
                raise ValueError("github_unavailable")
            batch = json.loads(response.stdout)
            if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
                raise ValueError("invalid_events")
            for item in batch:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0 or item["id"] in seen:
                    raise ValueError("invalid_event_identity")
                seen.add(item["id"])
                rows.append(item)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as exc:
            raise IssueRejected("pickup_events_unavailable") from exc
        if len(batch) < PAGE_SIZE:
            return rows
    raise IssueRejected("pickup_events_incomplete")


def candidates(node, *, searcher=None):
    """Discover exact node candidates. Discovery itself grants no authority."""
    if not isinstance(node, str) or not NODE.fullmatch(node):
        raise IssueRejected("invalid_node")
    from .github_search import search_issues
    rows = (searcher or search_issues)(label=node)
    return sorted({f'{row["repository"]}#{row["number"]}' for row in rows
                   if node in row["labels"] and "on-hold" not in
                   {label.casefold() for label in row["labels"]}})
