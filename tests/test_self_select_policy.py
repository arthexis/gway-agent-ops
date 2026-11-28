from datetime import datetime, timedelta, timezone

from gway_agent_ops.self_select_policy import select

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)


def candidate(number, *, age=4, labels=(), kind="issue", draft=False, repo="arthexis/example"):
    return {"repository": repo, "number": number, "updated_at": (NOW - timedelta(hours=age)).isoformat(),
            "labels": list(labels), "kind": kind, "draft": draft, "state": "open"}


def test_prefers_labeled_priority_and_filters_recent_and_held():
    rows = [candidate(1, labels=["critical"], age=2), candidate(2, labels=["critical", "on-hold"]),
            candidate(3, labels=["critical", "on-hold"], kind="pull_request", draft=True),
            candidate(4, age=9), candidate(5, labels=["bug"], kind="pull_request")]
    assert select(rows, now=NOW)["number"] == 5


def test_draft_prs_are_eligible_for_work():
    assert select([candidate(3, kind="pull_request", draft=True, labels=["do-not-merge"])], now=NOW)["number"] == 3


def test_exact_boundary_owner_and_invalid_input_are_safe():
    rows = [candidate(1, repo="another/example"), {"bad": "data"}, candidate(2, age=3)]
    assert select(rows, now=NOW)["number"] == 2
    assert select([candidate(1, age=2)], now=NOW) is None


def test_prefers_more_recent_stale_item_at_same_priority():
    assert select([candidate(1, age=200), candidate(2, age=4)], now=NOW)["number"] == 2
