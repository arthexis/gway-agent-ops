import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gway_agent_ops.self_select_discovery import discover

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)


def result(items, total=None, incomplete=False):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"total_count": len(items) if total is None else total,
        "incomplete_results": incomplete, "items": items}))


def item():
    return {"repository_url": "https://api.github.com/repos/arthexis/test", "number": 1,
            "state": "open", "updated_at": "2026-11-27T08:00:00Z", "labels": []}


def test_discovery_requests_archived_exclusion_and_normalizes_issue():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return result([item()])
    rows = discover(now=NOW, runner=run)
    assert rows[0]["repository"] == "arthexis/test"
    assert rows[0]["kind"] == "issue"
    assert any("archived:false" in arg for arg in calls[0])


def test_incomplete_pagination_and_unverified_draft_fail_closed():
    with pytest.raises(ValueError):
        discover(now=NOW, runner=lambda *a, **k: result([item()], total=2))
    with pytest.raises(ValueError):
        discover(now=NOW, runner=lambda *a, **k: result([item()], incomplete=True))
    pr = {**item(), "pull_request": {}}
    with pytest.raises(ValueError):
        discover(now=NOW, runner=lambda *a, **k: result([pr]))


def test_foreign_repo_and_failed_request_fail_closed():
    foreign = {**item(), "repository_url": "https://api.github.com/repos/other/test"}
    with pytest.raises(ValueError):
        discover(now=NOW, runner=lambda *a, **k: result([foreign]))
    with pytest.raises(ValueError):
        discover(now=NOW, runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
