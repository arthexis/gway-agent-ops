"""Pure ranking for explicitly authorized cross-repository background work.

Inputs are already authenticated, complete GitHub observations. This module
does not discover, authorize, claim, or execute candidates.
"""
from datetime import datetime, timedelta, timezone

from .pickup_policy import _instant

EXCLUDE = frozenset({"on-hold", "blocked", "wontfix"})
RANK = {"critical": 0, "priority: critical": 0, "p0": 0,
        "high priority": 1, "priority: high": 1, "p1": 1,
        "bug": 2, "security": 1}


def select(candidates, *, now, owner="arthexis", idle=timedelta(hours=3)):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    if not isinstance(candidates, list):
        raise ValueError("invalid_candidates")
    now = now.astimezone(timezone.utc)
    eligible = []
    for item in candidates:
        try:
            repo = item["repository"]
            labels = item["labels"]
            number = item["number"]
            updated = _instant(item["updated_at"])
            if (not isinstance(repo, str) or len(repo.split("/")) != 2
                    or repo.split("/")[0].casefold() != owner.casefold()
                    or type(number) is not int or number < 1
                    or item["kind"] not in ("issue", "pull_request")
                    or item["state"] != "open" or type(labels) is not list
                    or any(not isinstance(label, str) for label in labels)
                    or type(item["draft"]) is not bool):
                continue
            names = {label.casefold() for label in labels}
            if names & EXCLUDE or updated > now - idle:
                continue
            priority = min((RANK[x] for x in names if x in RANK), default=3)
            # Prefer recently active (but already stale) work within a priority
            # class. Very old issues can be superseded or externally blocked.
            eligible.append((priority, -updated.timestamp(), repo.casefold(), number, item))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return min(eligible, key=lambda row: row[:4])[-1] if eligible else None
