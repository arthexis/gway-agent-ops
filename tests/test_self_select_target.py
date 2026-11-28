from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from gway_agent_ops.github_issues import IssueRejected
from gway_agent_ops.self_select_target import resolve

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)


def runner_for(*responses):
    values = iter(responses)
    return lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(next(values)))


REPO = {"full_name": "arthexis/example", "archived": False}
ISSUE = {"number": 9, "title": "Fix", "body": "details", "comments": 0,
         "labels": [], "state": "open", "updated_at": "2026-11-27T08:00:00Z"}
PR = {"number": 9, "state": "open", "draft": False,
      "updated_at": "2026-11-27T08:30:00Z", "head": {"sha": "a" * 40}}


def test_issue_and_pull_are_separate_fresh_targets():
    issue = resolve("arthexis/example#9", now=NOW, runner=runner_for(REPO, ISSUE, []))
    assert issue["kind"] == "issue" and issue["head_sha"] is None
    pr = resolve("arthexis/example#9", now=NOW, runner=runner_for(REPO, {**ISSUE, "pull_request": {}}, PR, []))
    assert pr["kind"] == "pull_request" and pr["head_sha"] == "a" * 40


def test_recent_pr_or_hold_or_archived_fails_closed():
    for repository, issue, pr in [
        (REPO, {**ISSUE, "pull_request": {}}, {**PR, "updated_at": "2026-11-27T10:00:00Z"}),
        (REPO, {**ISSUE, "pull_request": {}, "labels": [{"name": "on-hold"}]}, PR),
    ]:
        with pytest.raises(IssueRejected):
            resolve("arthexis/example#9", now=NOW, runner=runner_for(repository, issue, pr))
    with pytest.raises(IssueRejected):
        resolve("arthexis/example#9", now=NOW, runner=runner_for({**REPO, "archived": True}, ISSUE))
    with pytest.raises(IssueRejected):
        resolve("other/example#9", now=NOW, runner=runner_for())


def test_recently_updated_related_open_item_blocks_but_old_or_closed_does_not():
    cross = {"event": "cross-referenced", "source": {"issue": {"state": "open", "updated_at": "2026-11-27T10:00:00Z"}}}
    with pytest.raises(IssueRejected, match="target_ineligible"):
        resolve("arthexis/example#9", now=NOW, runner=runner_for(REPO, ISSUE, [cross]))
    cross["source"]["issue"]["state"] = "closed"
    assert resolve("arthexis/example#9", now=NOW, runner=runner_for(REPO, ISSUE, [cross]))["number"] == 9
