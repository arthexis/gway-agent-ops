"""Bounded GitHub search for an explicitly authorized self-selecting task.

Search is discovery only. Re-fetch the selected item, quota and admission under
the submission gate immediately before dispatch; search results are not an
execution authority. Never treat an incomplete search as an empty result.
"""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

MAX_PAGES = 10


def discover(*, now, owner="arthexis", runner=subprocess.run):
    if (not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None or owner != "arthexis"):
        raise ValueError("invalid_discovery_context")
    before = (now.astimezone(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"user:{owner} is:open archived:false updated:<={before}"
    rows = []
    expected = None
    for page in range(1, MAX_PAGES + 1):
        try:
            proc = runner(["gh", "api", "--method", "GET", "search/issues",
                           "-f", "q=" + query, "-f", "per_page=100", "-f", f"page={page}"],
                          capture_output=True, text=True, timeout=30, check=False,
                          env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
            if proc.returncode:
                raise ValueError("github_unavailable")
            payload = json.loads(proc.stdout)
            total = payload["total_count"]
            batch = payload["items"]
            if (payload.get("incomplete_results") is not False or type(total) is not int
                    or total < 0 or total > 1000 or type(batch) is not list or len(batch) > 100
                    or (expected is not None and total != expected)):
                raise ValueError("incomplete_discovery")
            expected = total
            for item in batch:
                url = urlsplit(item["repository_url"])
                parts = url.path.strip("/").split("/")
                if (url.scheme != "https" or url.netloc != "api.github.com" or url.query or url.fragment
                        or len(parts) != 3 or parts[0] != "repos" or parts[1].casefold() != owner.casefold()
                        or not parts[2]):
                    raise ValueError("invalid_repository")
                labels = item["labels"]
                if not isinstance(labels, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str) for x in labels):
                    raise ValueError("invalid_labels")
                is_pr = "pull_request" in item
                # Search omits draft in some responses. Unknown is never assumed ready.
                draft = item.get("draft") if is_pr else False
                if is_pr and type(draft) is not bool:
                    raise ValueError("draft_state_unavailable")
                rows.append({"repository": parts[1] + "/" + parts[2], "number": item["number"],
                             "updated_at": item["updated_at"], "state": item["state"],
                             "kind": "pull_request" if is_pr else "issue", "draft": draft,
                             "labels": [x["name"] for x in labels]})
            if len(rows) >= total:
                if len(rows) != total:
                    raise ValueError("incomplete_discovery")
                return rows
            if len(batch) < 100:
                raise ValueError("incomplete_discovery")
        except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("discovery_unavailable") from exc
    raise ValueError("incomplete_discovery")
