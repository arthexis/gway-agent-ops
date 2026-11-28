"""Fresh, separately authorized target resolution for the self-select task.

Unlike ordinary issue admission, this resolver can return PRs and does not
trust the item author as an instruction source. Its caller must bind it to a
durable self-select claim and revalidate immediately before launch.
"""
from datetime import datetime, timezone
import json
import os
import subprocess

from .cli import parse_issue_ref
from .github_issues import IssueRejected
from .self_select_policy import select


def resolve(ref, *, repo=None, now=None, runner=subprocess.run):
    repository, number = parse_issue_ref(ref, repo)
    if repository.split("/")[0].casefold() != "arthexis":
        raise IssueRejected("repository_not_owned")

    def get(path):
        try:
            response = runner(["gh", "api", "--hostname", "github.com", path],
                capture_output=True, text=True, timeout=20, check=False,
                env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
            if response.returncode:
                raise ValueError("unavailable")
            data = json.loads(response.stdout)
            if not isinstance(data, dict):
                raise ValueError("invalid")
            return data
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
            raise IssueRejected("github_unavailable") from exc

    repository_data = get("repos/" + repository)
    issue = get(f"repos/{repository}/issues/{number}")
    try:
        if (repository_data["full_name"].casefold() != repository.casefold()
                or repository_data["archived"] is not False
                or issue["number"] != number
                or not isinstance(issue["title"], str)
                or not isinstance(issue.get("body"), (str, type(None)))
                or type(issue["comments"]) is not int or issue["comments"] < 0):
            raise ValueError("invalid_target")
        labels = issue["labels"]
        if not isinstance(labels, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str) for x in labels):
            raise ValueError("invalid_labels")
        kind = "pull_request" if "pull_request" in issue else "issue"
        draft = False
        head = None
        updated = issue["updated_at"]
        if kind == "pull_request":
            pr = get(f"repos/{repository}/pulls/{number}")
            if (pr["number"] != number or pr["state"] != "open"
                    or type(pr["draft"]) is not bool
                    or not isinstance(pr["head"]["sha"], str)
                    or len(pr["head"]["sha"]) != 40
                    or any(c not in "0123456789abcdef" for c in pr["head"]["sha"])):
                raise ValueError("invalid_pr")
            draft, head = pr["draft"], pr["head"]["sha"]
            # A PR can change independently of its issue endpoint.
            from .pickup_policy import _instant
            updated = max((updated, pr["updated_at"]), key=_instant)
        candidate = {"repository": repository, "number": number, "updated_at": updated,
                     "state": issue["state"], "kind": kind, "draft": draft,
                     "labels": [x["name"] for x in labels]}
        if select([candidate], now=now or datetime.now(timezone.utc)) is None:
            raise IssueRejected("target_ineligible")
        return {**candidate, "title": issue["title"], "body": issue.get("body") or "",
                "comments": issue["comments"], "head_sha": head,
                "url": f"https://github.com/{repository}/{'pull' if kind == 'pull_request' else 'issues'}/{number}"}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, IssueRejected):
            raise
        raise IssueRejected("invalid_target") from exc
